import base64
import copy
import glob
import json
import logging
import os
import re
import subprocess as sp
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import reduce
from pathlib import Path
from urllib.parse import unquote

import cv2
import numpy as np
import pandas as pd
import pytz
import requests
from flask import (
    Blueprint,
    Flask,
    Response,
    current_app,
    jsonify,
    make_response,
    request,
)
from markupsafe import escape
from peewee import DoesNotExist, fn, operator
from playhouse.shortcuts import model_to_dict
from playhouse.sqliteq import SqliteQueueDatabase
from tzlocal import get_localzone_name
from werkzeug.utils import secure_filename

from frigate.config import FrigateConfig
from frigate.const import (
    CACHE_DIR,
    CLIPS_DIR,
    CONFIG_DIR,
    EXPORT_DIR,
    MAX_SEGMENT_DURATION,
    RECORD_DIR,
)
from frigate.events.external import ExternalEventProcessor
from frigate.models import Event, Previews, Recordings, Regions, Timeline
from frigate.object_processing import TrackedObject
from frigate.plus import PlusApi
from frigate.ptz.onvif import OnvifController
from frigate.record.export import PlaybackFactorEnum, RecordingExporter
from frigate.stats import stats_snapshot
from frigate.storage import StorageMaintainer
from frigate.util.builtin import (
    clean_camera_user_pass,
    get_tz_modifiers,
    update_yaml_from_url,
)
from frigate.util.services import ffprobe_stream, restart_frigate, vainfo_hwaccel
from frigate.version import VERSION

logger = logging.getLogger(__name__)

DEFAULT_TIME_RANGE = "00:00,24:00"

bp = Blueprint("frigate", __name__)


def create_app(
    frigate_config,
    database: SqliteQueueDatabase,
    stats_tracking,
    detected_frames_processor,
    storage_maintainer: StorageMaintainer,
    onvif: OnvifController,
    external_processor: ExternalEventProcessor,
    plus_api: PlusApi,
):
    app = Flask(__name__)

    @app.before_request
    def check_csrf():
        if request.method in ["GET", "HEAD", "OPTIONS", "TRACE"]:
            pass
        if "origin" in request.headers and "x-csrf-token" not in request.headers:
            return jsonify({"success": False, "message": "Missing CSRF header"}), 401

    @app.before_request
    def _db_connect():
        if database.is_closed():
            database.connect()

    @app.teardown_request
    def _db_close(exc):
        if not database.is_closed():
            database.close()

    app.frigate_config = frigate_config
    app.stats_tracking = stats_tracking
    app.detected_frames_processor = detected_frames_processor
    app.storage_maintainer = storage_maintainer
    app.onvif = onvif
    app.external_processor = external_processor
    app.plus_api = plus_api
    app.camera_error_image = None
    app.hwaccel_errors = []

    app.register_blueprint(bp)

    return app


@bp.route("/")
def is_healthy():
    return "Frigate is running. Alive and healthy!"


@bp.route("/events/summary")
def events_summary():
    tz_name = request.args.get("timezone", default="utc", type=str)
    hour_modifier, minute_modifier, seconds_offset = get_tz_modifiers(tz_name)
    has_clip = request.args.get("has_clip", type=int)
    has_snapshot = request.args.get("has_snapshot", type=int)

    clauses = []

    if has_clip is not None:
        clauses.append((Event.has_clip == has_clip))

    if has_snapshot is not None:
        clauses.append((Event.has_snapshot == has_snapshot))

    if len(clauses) == 0:
        clauses.append((True))

    groups = (
        Event.select(
            Event.camera,
            Event.label,
            Event.sub_label,
            fn.strftime(
                "%Y-%m-%d",
                fn.datetime(
                    Event.start_time, "unixepoch", hour_modifier, minute_modifier
                ),
            ).alias("day"),
            Event.zones,
            fn.COUNT(Event.id).alias("count"),
        )
        .where(reduce(operator.and_, clauses))
        .group_by(
            Event.camera,
            Event.label,
            Event.sub_label,
            (Event.start_time + seconds_offset).cast("int") / (3600 * 24),
            Event.zones,
        )
    )

    return jsonify([e for e in groups.dicts()])


@bp.route("/events/<id>", methods=("GET",))
def event(id):
    try:
        return model_to_dict(Event.get(Event.id == id))
    except DoesNotExist:
        return "Event not found", 404


@bp.route("/events/<id>/retain", methods=("POST",))
def set_retain(id):
    try:
        event = Event.get(Event.id == id)
    except DoesNotExist:
        return make_response(
            jsonify({"success": False, "message": "Event " + id + " not found"}), 404
        )

    event.retain_indefinitely = True
    event.save()

    return make_response(
        jsonify({"success": True, "message": "Event " + id + " retained"}), 200
    )


@bp.route("/events/<id>/plus", methods=("POST",))
def send_to_plus(id):
    if not current_app.plus_api.is_active():
        message = "PLUS_API_KEY environment variable is not set"
        logger.error(message)
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": message,
                }
            ),
            400,
        )

    include_annotation = (
        request.json.get("include_annotation") if request.is_json else None
    )

    try:
        event = Event.get(Event.id == id)
    except DoesNotExist:
        message = f"Event {id} not found"
        logger.error(message)
        return make_response(jsonify({"success": False, "message": message}), 404)

    # events from before the conversion to relative dimensions cant include annotations
    if event.data.get("box") is None:
        include_annotation = None

    if event.end_time is None:
        logger.error(f"Unable to load clean png for in-progress event: {event.id}")
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": "Unable to load clean png for in-progress event",
                }
            ),
            400,
        )

    if event.plus_id:
        message = "Already submitted to plus"
        logger.error(message)
        return make_response(jsonify({"success": False, "message": message}), 400)

    # load clean.png
    try:
        filename = f"{event.camera}-{event.id}-clean.png"
        image = cv2.imread(os.path.join(CLIPS_DIR, filename))
    except Exception:
        logger.error(f"Unable to load clean png for event: {event.id}")
        return make_response(
            jsonify(
                {"success": False, "message": "Unable to load clean png for event"}
            ),
            400,
        )

    if image is None or image.size == 0:
        logger.error(f"Unable to load clean png for event: {event.id}")
        return make_response(
            jsonify(
                {"success": False, "message": "Unable to load clean png for event"}
            ),
            400,
        )

    try:
        plus_id = current_app.plus_api.upload_image(image, event.camera)
    except Exception as ex:
        logger.exception(ex)
        return make_response(
            jsonify({"success": False, "message": "Error uploading image"}),
            400,
        )

    # store image id in the database
    event.plus_id = plus_id
    event.save()

    if include_annotation is not None:
        box = event.data["box"]

        try:
            current_app.plus_api.add_annotation(
                event.plus_id,
                box,
                event.label,
            )
        except ValueError:
            message = "Error uploading annotation, unsupported label provided."
            logger.error(message)
            return make_response(
                jsonify({"success": False, "message": message}),
                400,
            )
        except Exception as ex:
            logger.exception(ex)
            return make_response(
                jsonify({"success": False, "message": "Error uploading annotation"}),
                400,
            )

    return make_response(jsonify({"success": True, "plus_id": plus_id}), 200)


@bp.route("/events/<id>/false_positive", methods=("PUT",))
def false_positive(id):
    if not current_app.plus_api.is_active():
        message = "PLUS_API_KEY environment variable is not set"
        logger.error(message)
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": message,
                }
            ),
            400,
        )

    try:
        event = Event.get(Event.id == id)
    except DoesNotExist:
        message = f"Event {id} not found"
        logger.error(message)
        return make_response(jsonify({"success": False, "message": message}), 404)

    # events from before the conversion to relative dimensions cant include annotations
    if event.data.get("box") is None:
        message = "Events prior to 0.13 cannot be submitted as false positives"
        logger.error(message)
        return make_response(jsonify({"success": False, "message": message}), 400)

    if event.false_positive:
        message = "False positive already submitted to Frigate+"
        logger.error(message)
        return make_response(jsonify({"success": False, "message": message}), 400)

    if not event.plus_id:
        plus_response = send_to_plus(id)
        if plus_response.status_code != 200:
            return plus_response
        # need to refetch the event now that it has a plus_id
        event = Event.get(Event.id == id)

    region = event.data["region"]
    box = event.data["box"]

    # provide top score if score is unavailable
    score = (
        (event.data["top_score"] if event.data["top_score"] else event.top_score)
        if event.data["score"] is None
        else event.data["score"]
    )

    try:
        current_app.plus_api.add_false_positive(
            event.plus_id,
            region,
            box,
            score,
            event.label,
            event.model_hash,
            event.model_type,
            event.detector_type,
        )
    except ValueError:
        message = "Error uploading false positive, unsupported label provided."
        logger.error(message)
        return make_response(
            jsonify({"success": False, "message": message}),
            400,
        )
    except Exception as ex:
        logger.exception(ex)
        return make_response(
            jsonify({"success": False, "message": "Error uploading false positive"}),
            400,
        )

    event.false_positive = True
    event.save()

    return make_response(jsonify({"success": True, "plus_id": event.plus_id}), 200)


@bp.route("/events/<id>/retain", methods=("DELETE",))
def delete_retain(id):
    try:
        event = Event.get(Event.id == id)
    except DoesNotExist:
        return make_response(
            jsonify({"success": False, "message": "Event " + id + " not found"}), 404
        )

    event.retain_indefinitely = False
    event.save()

    return make_response(
        jsonify({"success": True, "message": "Event " + id + " un-retained"}), 200
    )


@bp.route("/events/<id>/sub_label", methods=("POST",))
def set_sub_label(id):
    try:
        event: Event = Event.get(Event.id == id)
    except DoesNotExist:
        return make_response(
            jsonify({"success": False, "message": "Event " + id + " not found"}), 404
        )

    json: dict[str, any] = request.get_json(silent=True) or {}
    new_sub_label = json.get("subLabel")
    new_score = json.get("subLabelScore")

    if new_sub_label is None:
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": "A sub label must be supplied",
                }
            ),
            400,
        )

    if new_sub_label and len(new_sub_label) > 100:
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": new_sub_label
                    + " exceeds the 100 character limit for sub_label",
                }
            ),
            400,
        )

    if new_score is not None and (new_score > 1.0 or new_score < 0):
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": new_score
                    + " does not fit within the expected bounds 0 <= score <= 1.0",
                }
            ),
            400,
        )

    if not event.end_time:
        # update tracked object
        tracked_obj: TrackedObject = (
            current_app.detected_frames_processor.camera_states[
                event.camera
            ].tracked_objects.get(event.id)
        )

        if tracked_obj:
            tracked_obj.obj_data["sub_label"] = (new_sub_label, new_score)

        # update timeline items
        Timeline.update(
            data=Timeline.data.update({"sub_label": (new_sub_label, new_score)})
        ).where(Timeline.source_id == id).execute()

    event.sub_label = new_sub_label

    if new_score:
        data = event.data
        data["sub_label_score"] = new_score
        event.data = data

    event.save()
    return make_response(
        jsonify(
            {
                "success": True,
                "message": "Event " + id + " sub label set to " + new_sub_label,
            }
        ),
        200,
    )


@bp.route("/labels")
def get_labels():
    camera = request.args.get("camera", type=str, default="")

    try:
        if camera:
            events = Event.select(Event.label).where(Event.camera == camera).distinct()
        else:
            events = Event.select(Event.label).distinct()
    except Exception as e:
        logger.error(e)
        return make_response(
            jsonify({"success": False, "message": "Failed to get labels"}), 404
        )

    labels = sorted([e.label for e in events])
    return jsonify(labels)


@bp.route("/sub_labels")
def get_sub_labels():
    split_joined = request.args.get("split_joined", type=int)

    try:
        events = Event.select(Event.sub_label).distinct()
    except Exception:
        return make_response(
            jsonify({"success": False, "message": "Failed to get sub_labels"}),
            404,
        )

    sub_labels = [e.sub_label for e in events]

    if None in sub_labels:
        sub_labels.remove(None)

    if split_joined:
        original_labels = sub_labels.copy()

        for label in original_labels:
            if "," in label:
                sub_labels.remove(label)
                parts = label.split(",")

                for part in parts:
                    if part.strip() not in sub_labels:
                        sub_labels.append(part.strip())

    sub_labels.sort()
    return jsonify(sub_labels)


@bp.route("/events/<id>", methods=("DELETE",))
def delete_event(id):
    try:
        event = Event.get(Event.id == id)
    except DoesNotExist:
        return make_response(
            jsonify({"success": False, "message": "Event " + id + " not found"}), 404
        )

    media_name = f"{event.camera}-{event.id}"
    if event.has_snapshot:
        media = Path(f"{os.path.join(CLIPS_DIR, media_name)}.jpg")
        media.unlink(missing_ok=True)
        media = Path(f"{os.path.join(CLIPS_DIR, media_name)}-clean.png")
        media.unlink(missing_ok=True)
    if event.has_clip:
        media = Path(f"{os.path.join(CLIPS_DIR, media_name)}.mp4")
        media.unlink(missing_ok=True)

    event.delete_instance()
    Timeline.delete().where(Timeline.source_id == id).execute()
    return make_response(
        jsonify({"success": True, "message": "Event " + id + " deleted"}), 200
    )


@bp.route("/events/<id>/thumbnail.jpg")
def event_thumbnail(id, max_cache_age=2592000):
    format = request.args.get("format", "ios")
    thumbnail_bytes = None
    event_complete = False
    try:
        event = Event.get(Event.id == id)
        if event.end_time is not None:
            event_complete = True
        thumbnail_bytes = base64.b64decode(event.thumbnail)
    except DoesNotExist:
        # see if the object is currently being tracked
        try:
            camera_states = current_app.detected_frames_processor.camera_states.values()
            for camera_state in camera_states:
                if id in camera_state.tracked_objects:
                    tracked_obj = camera_state.tracked_objects.get(id)
                    if tracked_obj is not None:
                        thumbnail_bytes = tracked_obj.get_thumbnail()
        except Exception:
            return make_response(
                jsonify({"success": False, "message": "Event not found"}), 404
            )

    if thumbnail_bytes is None:
        return make_response(
            jsonify({"success": False, "message": "Event not found"}), 404
        )

    # android notifications prefer a 2:1 ratio
    if format == "android":
        jpg_as_np = np.frombuffer(thumbnail_bytes, dtype=np.uint8)
        img = cv2.imdecode(jpg_as_np, flags=1)
        thumbnail = cv2.copyMakeBorder(
            img,
            0,
            0,
            int(img.shape[1] * 0.5),
            int(img.shape[1] * 0.5),
            cv2.BORDER_CONSTANT,
            (0, 0, 0),
        )
        ret, jpg = cv2.imencode(".jpg", thumbnail, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        thumbnail_bytes = jpg.tobytes()

    response = make_response(thumbnail_bytes)
    response.headers["Content-Type"] = "image/jpeg"
    if event_complete:
        response.headers["Cache-Control"] = f"private, max-age={max_cache_age}"
    else:
        response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/events/<id>/preview.gif")
def event_preview(id: str, max_cache_age=2592000):
    try:
        event: Event = Event.get(Event.id == id)
    except DoesNotExist:
        return make_response(
            jsonify({"success": False, "message": "Event not found"}), 404
        )

    start_ts = event.start_time
    end_ts = (
        start_ts + min(event.end_time - event.start_time, 20) if event.end_time else 20
    )

    if datetime.fromtimestamp(event.start_time) < datetime.now().replace(
        minute=0, second=0
    ):
        # has preview mp4
        preview: Previews = (
            Previews.select(
                Previews.camera,
                Previews.path,
                Previews.duration,
                Previews.start_time,
                Previews.end_time,
            )
            .where(
                Previews.start_time.between(start_ts, end_ts)
                | Previews.end_time.between(start_ts, end_ts)
                | ((start_ts > Previews.start_time) & (end_ts < Previews.end_time))
            )
            .where(Previews.camera == event.camera)
            .limit(1)
            .get()
        )

        if not preview:
            return make_response(
                jsonify({"success": False, "message": "Preview not found"}), 404
            )

        diff = event.start_time - preview.start_time
        minutes = int(diff / 60)
        seconds = int(diff % 60)
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-ss",
            f"00:{minutes}:{seconds}",
            "-t",
            f"{end_ts - start_ts}",
            "-i",
            preview.path,
            "-r",
            "8",
            "-vf",
            "setpts=0.12*PTS",
            "-loop",
            "0",
            "-c:v",
            "gif",
            "-f",
            "gif",
            "-",
        ]

        process = sp.run(
            ffmpeg_cmd,
            capture_output=True,
        )

        if process.returncode != 0:
            logger.error(process.stderr)
            return make_response(
                jsonify({"success": False, "message": "Unable to create preview gif"}),
                500,
            )

        gif_bytes = process.stdout
    else:
        # need to generate from existing images
        preview_dir = os.path.join(CACHE_DIR, "preview_frames")
        file_start = f"preview_{event.camera}"
        start_file = f"{file_start}-{start_ts}.jpg"
        end_file = f"{file_start}-{end_ts}.jpg"
        selected_previews = []

        for file in sorted(os.listdir(preview_dir)):
            if not file.startswith(file_start):
                continue

            if file < start_file:
                continue

            if file > end_file:
                break

            selected_previews.append(f"file '/tmp/cache/preview_frames/{file}'")
            selected_previews.append("duration 0.12")

        if not selected_previews:
            return make_response(
                jsonify({"success": False, "message": "Preview not found"}), 404
            )

        last_file = selected_previews[-2]
        selected_previews.append(last_file)

        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "concat",
            "-y",
            "-protocol_whitelist",
            "pipe,file",
            "-safe",
            "0",
            "-i",
            "/dev/stdin",
            "-loop",
            "0",
            "-c:v",
            "gif",
            "-f",
            "gif",
            "-",
        ]

        process = sp.run(
            ffmpeg_cmd,
            input=str.encode("\n".join(selected_previews)),
            capture_output=True,
        )

        if process.returncode != 0:
            logger.error(process.stderr)
            return make_response(
                jsonify({"success": False, "message": "Unable to create preview gif"}),
                500,
            )

        gif_bytes = process.stdout

    response = make_response(gif_bytes)
    response.headers["Content-Type"] = "image/gif"
    response.headers["Cache-Control"] = f"private, max-age={max_cache_age}"
    return response


@bp.route("/timeline")
def timeline():
    camera = request.args.get("camera", "all")
    source_id = request.args.get("source_id", type=str)
    limit = request.args.get("limit", 100)

    clauses = []

    selected_columns = [
        Timeline.timestamp,
        Timeline.camera,
        Timeline.source,
        Timeline.source_id,
        Timeline.class_type,
        Timeline.data,
    ]

    if camera != "all":
        clauses.append((Timeline.camera == camera))

    if source_id:
        clauses.append((Timeline.source_id == source_id))

    if len(clauses) == 0:
        clauses.append((True))

    timeline = (
        Timeline.select(*selected_columns)
        .where(reduce(operator.and_, clauses))
        .order_by(Timeline.timestamp.asc())
        .limit(limit)
        .dicts()
    )

    return jsonify([t for t in timeline])


@bp.route("/timeline/hourly")
def hourly_timeline():
    """Get hourly summary for timeline."""
    cameras = request.args.get("cameras", "all")
    labels = request.args.get("labels", "all")
    before = request.args.get("before", type=float)
    after = request.args.get("after", type=float)
    limit = request.args.get("limit", 200)
    tz_name = request.args.get("timezone", default="utc", type=str)

    _, minute_modifier, _ = get_tz_modifiers(tz_name)
    minute_offset = int(minute_modifier.split(" ")[0])

    clauses = []

    if cameras != "all":
        camera_list = cameras.split(",")
        clauses.append((Timeline.camera << camera_list))

    if labels != "all":
        label_list = labels.split(",")
        clauses.append((Timeline.data["label"] << label_list))

    if before:
        clauses.append((Timeline.timestamp < before))

    if after:
        clauses.append((Timeline.timestamp > after))

    if len(clauses) == 0:
        clauses.append((True))

    timeline = (
        Timeline.select(
            Timeline.camera,
            Timeline.timestamp,
            Timeline.data,
            Timeline.class_type,
            Timeline.source_id,
            Timeline.source,
        )
        .where(reduce(operator.and_, clauses))
        .order_by(Timeline.timestamp.desc())
        .limit(limit)
        .dicts()
        .iterator()
    )

    count = 0
    start = 0
    end = 0
    hours: dict[str, list[dict[str, any]]] = {}

    for t in timeline:
        if count == 0:
            start = t["timestamp"]
        else:
            end = t["timestamp"]

        count += 1

        hour = (
            datetime.fromtimestamp(t["timestamp"]).replace(
                minute=0, second=0, microsecond=0
            )
            + timedelta(
                minutes=minute_offset,
            )
        ).timestamp()
        if hour not in hours:
            hours[hour] = [t]
        else:
            hours[hour].insert(0, t)

    return jsonify(
        {
            "start": start,
            "end": end,
            "count": count,
            "hours": hours,
        }
    )


@bp.route("/<camera_name>/recording/hourly/activity")
def hourly_timeline_activity(camera_name: str):
    """Get hourly summary for timeline."""
    if camera_name not in current_app.frigate_config.cameras:
        return make_response(
            jsonify({"success": False, "message": "Camera not found"}),
            404,
        )

    before = request.args.get("before", type=float, default=datetime.now())
    after = request.args.get(
        "after", type=float, default=datetime.now() - timedelta(hours=1)
    )
    tz_name = request.args.get("timezone", default="utc", type=str)

    _, minute_modifier, _ = get_tz_modifiers(tz_name)
    minute_offset = int(minute_modifier.split(" ")[0])

    all_recordings: list[Recordings] = (
        Recordings.select(
            Recordings.start_time,
            Recordings.duration,
            Recordings.objects,
            Recordings.motion,
        )
        .where(Recordings.camera == camera_name)
        .where(Recordings.motion > 0)
        .where((Recordings.start_time > after) & (Recordings.end_time < before))
        .order_by(Recordings.start_time.asc())
        .iterator()
    )

    # data format is ex:
    # {timestamp: [{ date: 1, count: 1, type: motion }]}] }}
    hours: dict[int, list[dict[str, any]]] = defaultdict(list)

    key = datetime.fromtimestamp(after).replace(second=0, microsecond=0) + timedelta(
        minutes=minute_offset
    )
    check = (key + timedelta(hours=1)).timestamp()

    # set initial start so data is representative of full hour
    hours[int(key.timestamp())].append(
        [
            key.timestamp(),
            0,
            False,
        ]
    )

    for recording in all_recordings:
        if recording.start_time > check:
            hours[int(key.timestamp())].append(
                [
                    (key + timedelta(minutes=59, seconds=59)).timestamp(),
                    0,
                    False,
                ]
            )
            key = key + timedelta(hours=1)
            check = (key + timedelta(hours=1)).timestamp()
            hours[int(key.timestamp())].append(
                [
                    key.timestamp(),
                    0,
                    False,
                ]
            )

        data_type = recording.objects > 0
        count = recording.motion + recording.objects
        hours[int(key.timestamp())].append(
            [
                recording.start_time + (recording.duration / 2),
                0 if count == 0 else np.log2(count),
                data_type,
            ]
        )

    # resample data using pandas to get activity on minute to minute basis
    for key, data in hours.items():
        df = pd.DataFrame(data, columns=["date", "count", "hasObjects"])

        # set date as datetime index
        df["date"] = pd.to_datetime(df["date"], unit="s")
        df.set_index(["date"], inplace=True)

        # normalize data
        df = df.resample("T").mean().fillna(0)

        # change types for output
        df.index = df.index.astype(int) // (10**9)
        df["count"] = df["count"].astype(int)
        df["hasObjects"] = df["hasObjects"].astype(bool)
        hours[key] = df.reset_index().to_dict("records")

    return jsonify(hours)


@bp.route("/<camera_name>/<label>/best.jpg")
@bp.route("/<camera_name>/<label>/thumbnail.jpg")
def label_thumbnail(camera_name, label):
    label = unquote(label)
    event_query = Event.select(fn.MAX(Event.id)).where(Event.camera == camera_name)
    if label != "any":
        event_query = event_query.where(Event.label == label)

    try:
        event = event_query.scalar()

        return event_thumbnail(event, 60)
    except DoesNotExist:
        frame = np.zeros((175, 175, 3), np.uint8)
        ret, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])

        response = make_response(jpg.tobytes())
        response.headers["Content-Type"] = "image/jpeg"
        response.headers["Cache-Control"] = "no-store"
        return response


@bp.route("/events/<id>/snapshot.jpg")
def event_snapshot(id):
    download = request.args.get("download", type=bool)
    event_complete = False
    jpg_bytes = None
    try:
        event = Event.get(Event.id == id, Event.end_time != None)
        event_complete = True
        if not event.has_snapshot:
            return make_response(
                jsonify({"success": False, "message": "Snapshot not available"}), 404
            )
        # read snapshot from disk
        with open(
            os.path.join(CLIPS_DIR, f"{event.camera}-{event.id}.jpg"), "rb"
        ) as image_file:
            jpg_bytes = image_file.read()
    except DoesNotExist:
        # see if the object is currently being tracked
        try:
            camera_states = current_app.detected_frames_processor.camera_states.values()
            for camera_state in camera_states:
                if id in camera_state.tracked_objects:
                    tracked_obj = camera_state.tracked_objects.get(id)
                    if tracked_obj is not None:
                        jpg_bytes = tracked_obj.get_jpg_bytes(
                            timestamp=request.args.get("timestamp", type=int),
                            bounding_box=request.args.get("bbox", type=int),
                            crop=request.args.get("crop", type=int),
                            height=request.args.get("h", type=int),
                            quality=request.args.get("quality", default=70, type=int),
                        )
        except Exception:
            return make_response(
                jsonify({"success": False, "message": "Event not found"}), 404
            )
    except Exception:
        return make_response(
            jsonify({"success": False, "message": "Event not found"}), 404
        )

    if jpg_bytes is None:
        return make_response(
            jsonify({"success": False, "message": "Event not found"}), 404
        )

    response = make_response(jpg_bytes)
    response.headers["Content-Type"] = "image/jpeg"
    if event_complete:
        response.headers["Cache-Control"] = "private, max-age=31536000"
    else:
        response.headers["Cache-Control"] = "no-store"
    if download:
        response.headers[
            "Content-Disposition"
        ] = f"attachment; filename=snapshot-{id}.jpg"
    return response


@bp.route("/<camera_name>/<label>/snapshot.jpg")
def label_snapshot(camera_name, label):
    label = unquote(label)
    if label == "any":
        event_query = (
            Event.select(Event.id)
            .where(Event.camera == camera_name)
            .where(Event.has_snapshot == True)
            .order_by(Event.start_time.desc())
        )
    else:
        event_query = (
            Event.select(Event.id)
            .where(Event.camera == camera_name)
            .where(Event.label == label)
            .where(Event.has_snapshot == True)
            .order_by(Event.start_time.desc())
        )

    try:
        event = event_query.get()
        return event_snapshot(event.id)
    except DoesNotExist:
        frame = np.zeros((720, 1280, 3), np.uint8)
        ret, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])

        response = make_response(jpg.tobytes())
        response.headers["Content-Type"] = "image/jpeg"
        return response


@bp.route("/<camera_name>/grid.jpg")
def grid_snapshot(camera_name):
    request.args.get("type", default="region")

    if camera_name in current_app.frigate_config.cameras:
        detect = current_app.frigate_config.cameras[camera_name].detect
        frame = current_app.detected_frames_processor.get_current_frame(camera_name, {})
        retry_interval = float(
            current_app.frigate_config.cameras.get(camera_name).ffmpeg.retry_interval
            or 10
        )

        if frame is None or datetime.now().timestamp() > (
            current_app.detected_frames_processor.get_current_frame_time(camera_name)
            + retry_interval
        ):
            return make_response(
                jsonify({"success": False, "message": "Unable to get valid frame"}),
                500,
            )

        try:
            grid = (
                Regions.select(Regions.grid)
                .where(Regions.camera == camera_name)
                .get()
                .grid
            )
        except DoesNotExist:
            return make_response(
                jsonify({"success": False, "message": "Unable to get region grid"}),
                500,
            )

        color_arg = request.args.get("color", default="", type=str).lower()
        draw_font_scale = request.args.get("font_scale", default=0.5, type=float)

        if color_arg == "red":
            draw_color = (0, 0, 255)
        elif color_arg == "blue":
            draw_color = (255, 0, 0)
        elif color_arg == "black":
            draw_color = (0, 0, 0)
        elif color_arg == "white":
            draw_color = (255, 255, 255)
        else:
            draw_color = (0, 255, 0)

        grid_size = len(grid)
        grid_coef = 1.0 / grid_size
        width = detect.width
        height = detect.height
        for x in range(grid_size):
            for y in range(grid_size):
                cell = grid[x][y]

                if len(cell["sizes"]) == 0:
                    continue

                std_dev = round(cell["std_dev"] * width, 2)
                mean = round(cell["mean"] * width, 2)
                cv2.rectangle(
                    frame,
                    (int(x * grid_coef * width), int(y * grid_coef * height)),
                    (
                        int((x + 1) * grid_coef * width),
                        int((y + 1) * grid_coef * height),
                    ),
                    draw_color,
                    2,
                )
                cv2.putText(
                    frame,
                    f"#: {len(cell['sizes'])}",
                    (
                        int(x * grid_coef * width + 10),
                        int((y * grid_coef + 0.02) * height),
                    ),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    fontScale=draw_font_scale,
                    color=draw_color,
                    thickness=2,
                )
                cv2.putText(
                    frame,
                    f"std: {std_dev}",
                    (
                        int(x * grid_coef * width + 10),
                        int((y * grid_coef + 0.05) * height),
                    ),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    fontScale=draw_font_scale,
                    color=draw_color,
                    thickness=2,
                )
                cv2.putText(
                    frame,
                    f"avg: {mean}",
                    (
                        int(x * grid_coef * width + 10),
                        int((y * grid_coef + 0.08) * height),
                    ),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    fontScale=draw_font_scale,
                    color=draw_color,
                    thickness=2,
                )

        ret, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        response = make_response(jpg.tobytes())
        response.headers["Content-Type"] = "image/jpeg"
        response.headers["Cache-Control"] = "no-store"
        return response
    else:
        return make_response(
            jsonify({"success": False, "message": "Camera not found"}),
            404,
        )


@bp.route("/events/<id>/clip.mp4")
def event_clip(id):
    download = request.args.get("download", type=bool)

    try:
        event: Event = Event.get(Event.id == id)
    except DoesNotExist:
        return make_response(
            jsonify({"success": False, "message": "Event not found"}), 404
        )

    if not event.has_clip:
        return make_response(
            jsonify({"success": False, "message": "Clip not available"}), 404
        )

    file_name = f"{event.camera}-{event.id}.mp4"
    clip_path = os.path.join(CLIPS_DIR, file_name)

    if not os.path.isfile(clip_path):
        end_ts = (
            datetime.now().timestamp() if event.end_time is None else event.end_time
        )
        return recording_clip(event.camera, event.start_time, end_ts)

    response = make_response()
    response.headers["Content-Description"] = "File Transfer"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Content-Type"] = "video/mp4"
    if download:
        response.headers["Content-Disposition"] = "attachment; filename=%s" % file_name
    response.headers["Content-Length"] = os.path.getsize(clip_path)
    response.headers[
        "X-Accel-Redirect"
    ] = f"/clips/{file_name}"  # nginx: https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_ignore_headers

    return response


@bp.route("/events")
def events():
    camera = request.args.get("camera", "all")
    cameras = request.args.get("cameras", "all")

    # handle old camera arg
    if cameras == "all" and camera != "all":
        cameras = camera

    label = unquote(request.args.get("label", "all"))
    labels = request.args.get("labels", "all")

    # handle old label arg
    if labels == "all" and label != "all":
        labels = label

    sub_label = request.args.get("sub_label", "all")
    sub_labels = request.args.get("sub_labels", "all")

    # handle old sub_label arg
    if sub_labels == "all" and sub_label != "all":
        sub_labels = sub_label

    zone = request.args.get("zone", "all")
    zones = request.args.get("zones", "all")

    # handle old label arg
    if zones == "all" and zone != "all":
        zones = zone

    limit = request.args.get("limit", 100)
    after = request.args.get("after", type=float)
    before = request.args.get("before", type=float)
    time_range = request.args.get("time_range", DEFAULT_TIME_RANGE)
    has_clip = request.args.get("has_clip", type=int)
    has_snapshot = request.args.get("has_snapshot", type=int)
    in_progress = request.args.get("in_progress", type=int)
    include_thumbnails = request.args.get("include_thumbnails", default=1, type=int)
    favorites = request.args.get("favorites", type=int)
    min_score = request.args.get("min_score", type=float)
    max_score = request.args.get("max_score", type=float)
    is_submitted = request.args.get("is_submitted", type=int)
    min_length = request.args.get("min_length", type=float)
    max_length = request.args.get("max_length", type=float)

    clauses = []

    selected_columns = [
        Event.id,
        Event.camera,
        Event.label,
        Event.zones,
        Event.start_time,
        Event.end_time,
        Event.has_clip,
        Event.has_snapshot,
        Event.plus_id,
        Event.retain_indefinitely,
        Event.sub_label,
        Event.top_score,
        Event.false_positive,
        Event.box,
        Event.data,
    ]

    if camera != "all":
        clauses.append((Event.camera == camera))

    if cameras != "all":
        camera_list = cameras.split(",")
        clauses.append((Event.camera << camera_list))

    if labels != "all":
        label_list = labels.split(",")
        clauses.append((Event.label << label_list))

    if sub_labels != "all":
        # use matching so joined sub labels are included
        # for example a sub label 'bob' would get events
        # with sub labels 'bob' and 'bob, john'
        sub_label_clauses = []
        filtered_sub_labels = sub_labels.split(",")

        if "None" in filtered_sub_labels:
            filtered_sub_labels.remove("None")
            sub_label_clauses.append((Event.sub_label.is_null()))

        for label in filtered_sub_labels:
            sub_label_clauses.append(
                (Event.sub_label.cast("text") == label)
            )  # include exact matches

            # include this label when part of a list
            sub_label_clauses.append((Event.sub_label.cast("text") % f"*{label},*"))
            sub_label_clauses.append((Event.sub_label.cast("text") % f"*, {label}*"))

        sub_label_clause = reduce(operator.or_, sub_label_clauses)
        clauses.append((sub_label_clause))

    if zones != "all":
        # use matching so events with multiple zones
        # still match on a search where any zone matches
        zone_clauses = []
        filtered_zones = zones.split(",")

        if "None" in filtered_zones:
            filtered_zones.remove("None")
            zone_clauses.append((Event.zones.length() == 0))

        for zone in filtered_zones:
            zone_clauses.append((Event.zones.cast("text") % f'*"{zone}"*'))

        zone_clause = reduce(operator.or_, zone_clauses)
        clauses.append((zone_clause))

    if after:
        clauses.append((Event.start_time > after))

    if before:
        clauses.append((Event.start_time < before))

    if time_range != DEFAULT_TIME_RANGE:
        # get timezone arg to ensure browser times are used
        tz_name = request.args.get("timezone", default="utc", type=str)
        hour_modifier, minute_modifier, _ = get_tz_modifiers(tz_name)

        times = time_range.split(",")
        time_after = times[0]
        time_before = times[1]

        start_hour_fun = fn.strftime(
            "%H:%M",
            fn.datetime(Event.start_time, "unixepoch", hour_modifier, minute_modifier),
        )

        # cases where user wants events overnight, ex: from 20:00 to 06:00
        # should use or operator
        if time_after > time_before:
            clauses.append(
                (
                    reduce(
                        operator.or_,
                        [(start_hour_fun > time_after), (start_hour_fun < time_before)],
                    )
                )
            )
        # all other cases should be and operator
        else:
            clauses.append((start_hour_fun > time_after))
            clauses.append((start_hour_fun < time_before))

    if has_clip is not None:
        clauses.append((Event.has_clip == has_clip))

    if has_snapshot is not None:
        clauses.append((Event.has_snapshot == has_snapshot))

    if in_progress is not None:
        clauses.append((Event.end_time.is_null(in_progress)))

    if include_thumbnails:
        selected_columns.append(Event.thumbnail)

    if favorites:
        clauses.append((Event.retain_indefinitely == favorites))

    if max_score is not None:
        clauses.append((Event.data["score"] <= max_score))

    if min_score is not None:
        clauses.append((Event.data["score"] >= min_score))

    if min_length is not None:
        clauses.append(((Event.end_time - Event.start_time) >= min_length))

    if max_length is not None:
        clauses.append(((Event.end_time - Event.start_time) <= max_length))

    if is_submitted is not None:
        if is_submitted == 0:
            clauses.append((Event.plus_id.is_null()))
        elif is_submitted > 0:
            clauses.append((Event.plus_id != ""))

    if len(clauses) == 0:
        clauses.append((True))

    events = (
        Event.select(*selected_columns)
        .where(reduce(operator.and_, clauses))
        .order_by(Event.start_time.desc())
        .limit(limit)
        .dicts()
        .iterator()
    )

    return jsonify(list(events))


@bp.route("/events/<camera_name>/<label>/create", methods=["POST"])
def create_event(camera_name, label):
    if not camera_name or not current_app.frigate_config.cameras.get(camera_name):
        return make_response(
            jsonify(
                {"success": False, "message": f"{camera_name} is not a valid camera."}
            ),
            404,
        )

    if not label:
        return make_response(
            jsonify({"success": False, "message": f"{label} must be set."}), 404
        )

    json: dict[str, any] = request.get_json(silent=True) or {}

    try:
        frame = current_app.detected_frames_processor.get_current_frame(camera_name)

        event_id = current_app.external_processor.create_manual_event(
            camera_name,
            label,
            json.get("source_type", "api"),
            json.get("sub_label", None),
            json.get("score", 0),
            json.get("duration", 30),
            json.get("include_recording", True),
            json.get("draw", {}),
            frame,
        )
    except Exception as e:
        logger.error(e)
        return make_response(
            jsonify({"success": False, "message": "An unknown error occurred"}),
            500,
        )

    return make_response(
        jsonify(
            {
                "success": True,
                "message": "Successfully created event.",
                "event_id": event_id,
            }
        ),
        200,
    )


@bp.route("/events/<event_id>/end", methods=["PUT"])
def end_event(event_id):
    json: dict[str, any] = request.get_json(silent=True) or {}

    try:
        end_time = json.get("end_time", datetime.now().timestamp())
        current_app.external_processor.finish_manual_event(event_id, end_time)
    except Exception:
        return make_response(
            jsonify(
                {"success": False, "message": f"{event_id} must be set and valid."}
            ),
            404,
        )

    return make_response(
        jsonify({"success": True, "message": "Event successfully ended."}), 200
    )


@bp.route("/config")
def config():
    config = current_app.frigate_config.dict()

    # remove the mqtt password
    config["mqtt"].pop("password", None)

    for camera_name, camera in current_app.frigate_config.cameras.items():
        camera_dict = config["cameras"][camera_name]

        # clean paths
        for input in camera_dict.get("ffmpeg", {}).get("inputs", []):
            input["path"] = clean_camera_user_pass(input["path"])

        # add clean ffmpeg_cmds
        camera_dict["ffmpeg_cmds"] = copy.deepcopy(camera.ffmpeg_cmds)
        for cmd in camera_dict["ffmpeg_cmds"]:
            cmd["cmd"] = clean_camera_user_pass(" ".join(cmd["cmd"]))

    config["plus"] = {"enabled": current_app.plus_api.is_active()}

    for detector, detector_config in config["detectors"].items():
        detector_config["model"][
            "labelmap"
        ] = current_app.frigate_config.model.merged_labelmap

    return jsonify(config)


@bp.route("/config/raw")
def config_raw():
    config_file = os.environ.get("CONFIG_FILE", "/config/config.yml")

    # Check if we can use .yaml instead of .yml
    config_file_yaml = config_file.replace(".yml", ".yaml")

    if os.path.isfile(config_file_yaml):
        config_file = config_file_yaml

    if not os.path.isfile(config_file):
        return make_response(
            jsonify({"success": False, "message": "Could not find file"}), 404
        )

    with open(config_file, "r") as f:
        raw_config = f.read()
        f.close()

        return raw_config, 200


@bp.route("/config/save", methods=["POST"])
def config_save():
    save_option = request.args.get("save_option")

    new_config = request.get_data().decode()

    if not new_config:
        return make_response(
            jsonify(
                {"success": False, "message": "Config with body param is required"}
            ),
            400,
        )

    # Validate the config schema
    try:
        FrigateConfig.parse_raw(new_config)
    except Exception:
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": f"\nConfig Error:\n\n{escape(str(traceback.format_exc()))}",
                }
            ),
            400,
        )

    # Save the config to file
    try:
        config_file = os.environ.get("CONFIG_FILE", "/config/config.yml")

        # Check if we can use .yaml instead of .yml
        config_file_yaml = config_file.replace(".yml", ".yaml")

        if os.path.isfile(config_file_yaml):
            config_file = config_file_yaml

        with open(config_file, "w") as f:
            f.write(new_config)
            f.close()
    except Exception:
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": "Could not write config file, be sure that Frigate has write permission on the config file.",
                }
            ),
            400,
        )

    if save_option == "restart":
        try:
            restart_frigate()
        except Exception as e:
            logging.error(f"Error restarting Frigate: {e}")
            return make_response(
                jsonify(
                    {
                        "success": True,
                        "message": "Config successfully saved, unable to restart Frigate",
                    }
                ),
                200,
            )

        return make_response(
            jsonify(
                {
                    "success": True,
                    "message": "Config successfully saved, restarting (this can take up to one minute)...",
                }
            ),
            200,
        )
    else:
        return make_response(
            jsonify({"success": True, "message": "Config successfully saved."}),
            200,
        )


@bp.route("/config/set", methods=["PUT"])
def config_set():
    config_file = os.environ.get("CONFIG_FILE", f"{CONFIG_DIR}/config.yml")

    # Check if we can use .yaml instead of .yml
    config_file_yaml = config_file.replace(".yml", ".yaml")

    if os.path.isfile(config_file_yaml):
        config_file = config_file_yaml

    with open(config_file, "r") as f:
        old_raw_config = f.read()
        f.close()

    try:
        update_yaml_from_url(config_file, request.url)
        with open(config_file, "r") as f:
            new_raw_config = f.read()
            f.close()
        # Validate the config schema
        try:
            FrigateConfig.parse_raw(new_raw_config)
        except Exception:
            with open(config_file, "w") as f:
                f.write(old_raw_config)
                f.close()
            logger.error(f"\nConfig Error:\n\n{str(traceback.format_exc())}")
            return make_response(
                jsonify(
                    {
                        "success": False,
                        "message": "Error parsing config. Check logs for error message.",
                    }
                ),
                400,
            )
    except Exception as e:
        logging.error(f"Error updating config: {e}")
        return make_response(
            jsonify({"success": False, "message": "Error updating config"}),
            500,
        )

    return make_response(
        jsonify(
            {
                "success": True,
                "message": "Config successfully updated, restart to apply",
            }
        ),
        200,
    )


@bp.route("/config/schema.json")
def config_schema():
    return current_app.response_class(
        current_app.frigate_config.schema_json(), mimetype="application/json"
    )


@bp.route("/go2rtc/streams")
def go2rtc_streams():
    r = requests.get("http://127.0.0.1:1984/api/streams")
    if not r.ok:
        logger.error("Failed to fetch streams from go2rtc")
        return make_response(
            jsonify({"success": False, "message": "Error fetching stream data"}),
            500,
        )
    stream_data = r.json()
    for data in stream_data.values():
        for producer in data.get("producers", []):
            producer["url"] = clean_camera_user_pass(producer.get("url", ""))
    return jsonify(stream_data)


@bp.route("/version")
def version():
    return VERSION


@bp.route("/stats")
def stats():
    stats = stats_snapshot(
        current_app.frigate_config,
        current_app.stats_tracking,
        current_app.hwaccel_errors,
    )
    return jsonify(stats)


@bp.route("/<camera_name>")
def mjpeg_feed(camera_name):
    fps = int(request.args.get("fps", "3"))
    height = int(request.args.get("h", "360"))
    draw_options = {
        "bounding_boxes": request.args.get("bbox", type=int),
        "timestamp": request.args.get("timestamp", type=int),
        "zones": request.args.get("zones", type=int),
        "mask": request.args.get("mask", type=int),
        "motion_boxes": request.args.get("motion", type=int),
        "regions": request.args.get("regions", type=int),
    }
    if camera_name in current_app.frigate_config.cameras:
        # return a multipart response
        return Response(
            imagestream(
                current_app.detected_frames_processor,
                camera_name,
                fps,
                height,
                draw_options,
            ),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )
    else:
        return make_response(
            jsonify({"success": False, "message": "Camera not found"}),
            404,
        )


@bp.route("/<camera_name>/ptz/info")
def camera_ptz_info(camera_name):
    if camera_name in current_app.frigate_config.cameras:
        return jsonify(current_app.onvif.get_camera_info(camera_name))
    else:
        return make_response(
            jsonify({"success": False, "message": "Camera not found"}),
            404,
        )


@bp.route("/<camera_name>/latest.jpg")
def latest_frame(camera_name):
    draw_options = {
        "bounding_boxes": request.args.get("bbox", type=int),
        "timestamp": request.args.get("timestamp", type=int),
        "zones": request.args.get("zones", type=int),
        "mask": request.args.get("mask", type=int),
        "motion_boxes": request.args.get("motion", type=int),
        "regions": request.args.get("regions", type=int),
    }
    resize_quality = request.args.get("quality", default=70, type=int)

    if camera_name in current_app.frigate_config.cameras:
        frame = current_app.detected_frames_processor.get_current_frame(
            camera_name, draw_options
        )
        retry_interval = float(
            current_app.frigate_config.cameras.get(camera_name).ffmpeg.retry_interval
            or 10
        )

        if frame is None or datetime.now().timestamp() > (
            current_app.detected_frames_processor.get_current_frame_time(camera_name)
            + retry_interval
        ):
            if current_app.camera_error_image is None:
                error_image = glob.glob("/opt/frigate/frigate/images/camera-error.jpg")

                if len(error_image) > 0:
                    current_app.camera_error_image = cv2.imread(
                        error_image[0], cv2.IMREAD_UNCHANGED
                    )

            frame = current_app.camera_error_image

        height = int(request.args.get("h", str(frame.shape[0])))
        width = int(height * frame.shape[1] / frame.shape[0])

        if frame is None:
            return make_response(
                jsonify({"success": False, "message": "Unable to get valid frame"}),
                500,
            )

        if height < 1 or width < 1:
            return (
                "Invalid height / width requested :: {} / {}".format(height, width),
                400,
            )

        frame = cv2.resize(frame, dsize=(width, height), interpolation=cv2.INTER_AREA)

        ret, jpg = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), resize_quality]
        )
        response = make_response(jpg.tobytes())
        response.headers["Content-Type"] = "image/jpeg"
        response.headers["Cache-Control"] = "no-store"
        return response
    elif camera_name == "birdseye" and current_app.frigate_config.birdseye.restream:
        frame = cv2.cvtColor(
            current_app.detected_frames_processor.get_current_frame(camera_name),
            cv2.COLOR_YUV2BGR_I420,
        )

        height = int(request.args.get("h", str(frame.shape[0])))
        width = int(height * frame.shape[1] / frame.shape[0])

        frame = cv2.resize(frame, dsize=(width, height), interpolation=cv2.INTER_AREA)

        ret, jpg = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), resize_quality]
        )
        response = make_response(jpg.tobytes())
        response.headers["Content-Type"] = "image/jpeg"
        response.headers["Cache-Control"] = "no-store"
        return response
    else:
        return make_response(
            jsonify({"success": False, "message": "Camera not found"}),
            404,
        )


@bp.route("/<camera_name>/recordings/<frame_time>/snapshot.png")
def get_snapshot_from_recording(camera_name: str, frame_time: str):
    if camera_name not in current_app.frigate_config.cameras:
        return make_response(
            jsonify({"success": False, "message": "Camera not found"}),
            404,
        )

    frame_time = float(frame_time)
    recording_query = (
        Recordings.select(
            Recordings.path,
            Recordings.start_time,
        )
        .where(
            (
                (frame_time >= Recordings.start_time)
                & (frame_time <= Recordings.end_time)
            )
        )
        .where(Recordings.camera == camera_name)
        .order_by(Recordings.start_time.desc())
        .limit(1)
    )

    try:
        recording: Recordings = recording_query.get()
        time_in_segment = frame_time - recording.start_time

        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-ss",
            f"00:00:{time_in_segment}",
            "-i",
            recording.path,
            "-frames:v",
            "1",
            "-c:v",
            "png",
            "-f",
            "image2pipe",
            "-",
        ]

        process = sp.run(
            ffmpeg_cmd,
            capture_output=True,
        )
        response = make_response(process.stdout)
        response.headers["Content-Type"] = "image/png"
        return response
    except DoesNotExist:
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": "Recording not found at {}".format(frame_time),
                }
            ),
            404,
        )


@bp.route("/recordings/storage", methods=["GET"])
def get_recordings_storage_usage():
    recording_stats = stats_snapshot(
        current_app.frigate_config,
        current_app.stats_tracking,
        current_app.hwaccel_errors,
    )["service"]["storage"][RECORD_DIR]

    if not recording_stats:
        return jsonify({})

    total_mb = recording_stats["total"]

    camera_usages: dict[
        str, dict
    ] = current_app.storage_maintainer.calculate_camera_usages()

    for camera_name in camera_usages.keys():
        if camera_usages.get(camera_name, {}).get("usage"):
            camera_usages[camera_name]["usage_percent"] = (
                camera_usages.get(camera_name, {}).get("usage", 0) / total_mb
            ) * 100

    return jsonify(camera_usages)


# return hourly summary for recordings of camera
@bp.route("/<camera_name>/recordings/summary")
def recordings_summary(camera_name):
    tz_name = request.args.get("timezone", default="utc", type=str)
    hour_modifier, minute_modifier, seconds_offset = get_tz_modifiers(tz_name)
    recording_groups = (
        Recordings.select(
            fn.strftime(
                "%Y-%m-%d %H",
                fn.datetime(
                    Recordings.start_time, "unixepoch", hour_modifier, minute_modifier
                ),
            ).alias("hour"),
            fn.SUM(Recordings.duration).alias("duration"),
            fn.SUM(Recordings.motion).alias("motion"),
            fn.SUM(Recordings.objects).alias("objects"),
        )
        .where(Recordings.camera == camera_name)
        .group_by((Recordings.start_time + seconds_offset).cast("int") / 3600)
        .order_by(Recordings.start_time.desc())
        .namedtuples()
    )

    event_groups = (
        Event.select(
            fn.strftime(
                "%Y-%m-%d %H",
                fn.datetime(
                    Event.start_time, "unixepoch", hour_modifier, minute_modifier
                ),
            ).alias("hour"),
            fn.COUNT(Event.id).alias("count"),
        )
        .where(Event.camera == camera_name, Event.has_clip)
        .group_by((Event.start_time + seconds_offset).cast("int") / 3600)
        .namedtuples()
    )

    event_map = {g.hour: g.count for g in event_groups}

    days = {}

    for recording_group in recording_groups:
        parts = recording_group.hour.split()
        hour = parts[1]
        day = parts[0]
        events_count = event_map.get(recording_group.hour, 0)
        hour_data = {
            "hour": hour,
            "events": events_count,
            "motion": recording_group.motion,
            "objects": recording_group.objects,
            "duration": round(recording_group.duration),
        }
        if day not in days:
            days[day] = {"events": events_count, "hours": [hour_data], "day": day}
        else:
            days[day]["events"] += events_count
            days[day]["hours"].append(hour_data)

    return jsonify(list(days.values()))


# return hour of recordings data for camera
@bp.route("/<camera_name>/recordings")
def recordings(camera_name):
    after = request.args.get(
        "after", type=float, default=(datetime.now() - timedelta(hours=1)).timestamp()
    )
    before = request.args.get("before", type=float, default=datetime.now().timestamp())

    recordings = (
        Recordings.select(
            Recordings.id,
            Recordings.start_time,
            Recordings.end_time,
            Recordings.segment_size,
            Recordings.motion,
            Recordings.objects,
            Recordings.duration,
        )
        .where(
            Recordings.camera == camera_name,
            Recordings.end_time >= after,
            Recordings.start_time <= before,
        )
        .order_by(Recordings.start_time)
        .dicts()
        .iterator()
    )

    return jsonify(list(recordings))


@bp.route("/<camera_name>/start/<int:start_ts>/end/<int:end_ts>/clip.mp4")
@bp.route("/<camera_name>/start/<float:start_ts>/end/<float:end_ts>/clip.mp4")
def recording_clip(camera_name, start_ts, end_ts):
    download = request.args.get("download", type=bool)

    recordings = (
        Recordings.select(
            Recordings.path,
            Recordings.start_time,
            Recordings.end_time,
        )
        .where(
            (Recordings.start_time.between(start_ts, end_ts))
            | (Recordings.end_time.between(start_ts, end_ts))
            | ((start_ts > Recordings.start_time) & (end_ts < Recordings.end_time))
        )
        .where(Recordings.camera == camera_name)
        .order_by(Recordings.start_time.asc())
    )

    playlist_lines = []
    clip: Recordings
    for clip in recordings:
        playlist_lines.append(f"file '{clip.path}'")
        # if this is the starting clip, add an inpoint
        if clip.start_time < start_ts:
            playlist_lines.append(f"inpoint {int(start_ts - clip.start_time)}")
        # if this is the ending clip, add an outpoint
        if clip.end_time > end_ts:
            playlist_lines.append(f"outpoint {int(end_ts - clip.start_time)}")

    file_name = secure_filename(f"clip_{camera_name}_{start_ts}-{end_ts}.mp4")
    path = os.path.join(CACHE_DIR, file_name)

    if not os.path.exists(path):
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-protocol_whitelist",
            "pipe,file",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            "/dev/stdin",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            path,
        ]
        p = sp.run(
            ffmpeg_cmd,
            input="\n".join(playlist_lines),
            encoding="ascii",
            capture_output=True,
        )

        if p.returncode != 0:
            logger.error(p.stderr)
            return make_response(
                jsonify(
                    {
                        "success": False,
                        "message": "Could not create clip from recordings",
                    }
                ),
                500,
            )
    else:
        logger.debug(
            f"Ignoring subsequent request for {path} as it already exists in the cache."
        )

    response = make_response()
    response.headers["Content-Description"] = "File Transfer"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Content-Type"] = "video/mp4"
    if download:
        response.headers["Content-Disposition"] = "attachment; filename=%s" % file_name
    response.headers["Content-Length"] = os.path.getsize(path)
    response.headers[
        "X-Accel-Redirect"
    ] = f"/cache/{file_name}"  # nginx: https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_ignore_headers

    return response


@bp.route("/vod/<camera_name>/start/<int:start_ts>/end/<int:end_ts>")
@bp.route("/vod/<camera_name>/start/<float:start_ts>/end/<float:end_ts>")
def vod_ts(camera_name, start_ts, end_ts):
    recordings = (
        Recordings.select(Recordings.path, Recordings.duration, Recordings.end_time)
        .where(
            Recordings.start_time.between(start_ts, end_ts)
            | Recordings.end_time.between(start_ts, end_ts)
            | ((start_ts > Recordings.start_time) & (end_ts < Recordings.end_time))
        )
        .where(Recordings.camera == camera_name)
        .order_by(Recordings.start_time.asc())
        .iterator()
    )

    clips = []
    durations = []
    max_duration_ms = MAX_SEGMENT_DURATION * 1000

    recording: Recordings
    for recording in recordings:
        clip = {"type": "source", "path": recording.path}
        duration = int(recording.duration * 1000)

        # Determine if we need to end the last clip early
        if recording.end_time > end_ts:
            duration -= int((recording.end_time - end_ts) * 1000)

        if 0 < duration < max_duration_ms:
            clip["keyFrameDurations"] = [duration]
            clips.append(clip)
            durations.append(duration)
        else:
            logger.warning(f"Recording clip is missing or empty: {recording.path}")

    if not clips:
        logger.error("No recordings found for the requested time range")
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": "No recordings found.",
                }
            ),
            404,
        )

    hour_ago = datetime.now() - timedelta(hours=1)
    return jsonify(
        {
            "cache": hour_ago.timestamp() > start_ts,
            "discontinuity": False,
            "consistentSequenceMediaInfo": True,
            "durations": durations,
            "segment_duration": max(durations),
            "sequences": [{"clips": clips}],
        }
    )


@bp.route("/vod/<year_month>/<day>/<hour>/<camera_name>")
def vod_hour_no_timezone(year_month, day, hour, camera_name):
    return vod_hour(
        year_month, day, hour, camera_name, get_localzone_name().replace("/", ",")
    )


@bp.route("/vod/<year_month>/<day>/<hour>/<camera_name>/<tz_name>")
def vod_hour(year_month, day, hour, camera_name, tz_name):
    parts = year_month.split("-")
    start_date = (
        datetime(int(parts[0]), int(parts[1]), int(day), int(hour), tzinfo=timezone.utc)
        - datetime.now(pytz.timezone(tz_name.replace(",", "/"))).utcoffset()
    )
    end_date = start_date + timedelta(hours=1) - timedelta(milliseconds=1)
    start_ts = start_date.timestamp()
    end_ts = end_date.timestamp()

    return vod_ts(camera_name, start_ts, end_ts)


@bp.route("/preview/<camera_name>/start/<int:start_ts>/end/<int:end_ts>")
@bp.route("/preview/<camera_name>/start/<float:start_ts>/end/<float:end_ts>")
def preview_ts(camera_name, start_ts, end_ts):
    """Get all mp4 previews relevant for time period."""
    if camera_name != "all":
        camera_clause = Previews.camera == camera_name
    else:
        camera_clause = True

    previews = (
        Previews.select(
            Previews.camera,
            Previews.path,
            Previews.duration,
            Previews.start_time,
            Previews.end_time,
        )
        .where(
            Previews.start_time.between(start_ts, end_ts)
            | Previews.end_time.between(start_ts, end_ts)
            | ((start_ts > Previews.start_time) & (end_ts < Previews.end_time))
        )
        .where(camera_clause)
        .order_by(Previews.start_time.asc())
        .dicts()
        .iterator()
    )

    clips = []

    preview: Previews
    for preview in previews:
        clips.append(
            {
                "camera": preview["camera"],
                "src": preview["path"].replace("/media/frigate", ""),
                "type": "video/mp4",
                "start": preview["start_time"],
                "end": preview["end_time"],
            }
        )

    if not clips:
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": "No previews found.",
                }
            ),
            404,
        )

    return make_response(jsonify(clips), 200)


@bp.route("/preview/<year_month>/<day>/<hour>/<camera_name>/<tz_name>")
def preview_hour(year_month, day, hour, camera_name, tz_name):
    parts = year_month.split("-")
    start_date = (
        datetime(int(parts[0]), int(parts[1]), int(day), int(hour), tzinfo=timezone.utc)
        - datetime.now(pytz.timezone(tz_name.replace(",", "/"))).utcoffset()
    )
    end_date = start_date + timedelta(hours=1) - timedelta(milliseconds=1)
    start_ts = start_date.timestamp()
    end_ts = end_date.timestamp()

    return preview_ts(camera_name, start_ts, end_ts)


@bp.route("/preview/<camera_name>/<frame_time>/thumbnail.jpg")
def preview_thumbnail(camera_name, frame_time):
    """Get a thumbnail from the cached preview jpgs."""
    preview_dir = os.path.join(CACHE_DIR, "preview_frames")
    file_start = f"preview_{camera_name}"
    file_check = f"{file_start}-{frame_time}.jpg"
    selected_preview = None

    for file in sorted(os.listdir(preview_dir)):
        if file.startswith(file_start):
            if file > file_check:
                selected_preview = file
                break

    if selected_preview is None:
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": "Could not find valid preview jpg.",
                }
            ),
            404,
        )

    with open(os.path.join(preview_dir, selected_preview), "rb") as image_file:
        jpg_bytes = image_file.read()

    response = make_response(jpg_bytes)
    response.headers["Content-Type"] = "image/jpeg"
    response.headers["Cache-Control"] = "private, max-age=31536000"
    return response


@bp.route("/vod/event/<id>")
def vod_event(id):
    try:
        event: Event = Event.get(Event.id == id)
    except DoesNotExist:
        logger.error(f"Event not found: {id}")
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": "Event not found.",
                }
            ),
            404,
        )

    if not event.has_clip:
        logger.error(f"Event does not have recordings: {id}")
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": "Recordings not available.",
                }
            ),
            404,
        )

    clip_path = os.path.join(CLIPS_DIR, f"{event.camera}-{event.id}.mp4")

    if not os.path.isfile(clip_path):
        end_ts = (
            datetime.now().timestamp() if event.end_time is None else event.end_time
        )
        vod_response = vod_ts(event.camera, event.start_time, end_ts)
        # If the recordings are not found and the event started more than 5 minutes ago, set has_clip to false
        if (
            event.start_time < datetime.now().timestamp() - 300
            and type(vod_response) == tuple
            and len(vod_response) == 2
            and vod_response[1] == 404
        ):
            Event.update(has_clip=False).where(Event.id == id).execute()
        return vod_response

    duration = int((event.end_time - event.start_time) * 1000)
    return jsonify(
        {
            "cache": True,
            "discontinuity": False,
            "durations": [duration],
            "sequences": [{"clips": [{"type": "source", "path": clip_path}]}],
        }
    )


@bp.route(
    "/export/<camera_name>/start/<int:start_time>/end/<int:end_time>", methods=["POST"]
)
@bp.route(
    "/export/<camera_name>/start/<float:start_time>/end/<float:end_time>",
    methods=["POST"],
)
def export_recording(camera_name: str, start_time, end_time):
    if not camera_name or not current_app.frigate_config.cameras.get(camera_name):
        return make_response(
            jsonify(
                {"success": False, "message": f"{camera_name} is not a valid camera."}
            ),
            404,
        )

    json: dict[str, any] = request.get_json(silent=True) or {}
    playback_factor = json.get("playback", "realtime")

    recordings_count = (
        Recordings.select()
        .where(
            Recordings.start_time.between(start_time, end_time)
            | Recordings.end_time.between(start_time, end_time)
            | ((start_time > Recordings.start_time) & (end_time < Recordings.end_time))
        )
        .where(Recordings.camera == camera_name)
        .count()
    )

    if recordings_count <= 0:
        return make_response(
            jsonify(
                {"success": False, "message": "No recordings found for time range"}
            ),
            400,
        )

    exporter = RecordingExporter(
        current_app.frigate_config,
        camera_name,
        int(start_time),
        int(end_time),
        (
            PlaybackFactorEnum[playback_factor]
            if playback_factor in PlaybackFactorEnum.__members__.values()
            else PlaybackFactorEnum.realtime
        ),
    )
    exporter.start()
    return make_response(
        jsonify(
            {
                "success": True,
                "message": "Starting export of recording.",
            }
        ),
        200,
    )


def export_filename_check_extension(filename: str):
    if filename.endswith(".mp4"):
        return filename
    else:
        return filename + ".mp4"


def export_filename_is_valid(filename: str):
    if re.search(r"[^:_A-Za-z0-9]", filename) or filename.startswith("in_progress."):
        return False
    else:
        return True


@bp.route("/export/<file_name_current>/<file_name_new>", methods=["PATCH"])
def export_rename(file_name_current, file_name_new: str):
    safe_file_name_current = secure_filename(
        export_filename_check_extension(file_name_current)
    )
    file_current = os.path.join(EXPORT_DIR, safe_file_name_current)

    if not os.path.exists(file_current):
        return make_response(
            jsonify({"success": False, "message": f"{file_name_current} not found."}),
            404,
        )

    if not export_filename_is_valid(file_name_new):
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": f"{file_name_new} contains illegal characters.",
                }
            ),
            400,
        )

    safe_file_name_new = secure_filename(export_filename_check_extension(file_name_new))
    file_new = os.path.join(EXPORT_DIR, safe_file_name_new)

    if os.path.exists(file_new):
        return make_response(
            jsonify({"success": False, "message": f"{file_name_new} already exists."}),
            400,
        )

    os.rename(file_current, file_new)
    return make_response(
        jsonify(
            {
                "success": True,
                "message": "Successfully renamed file.",
            }
        ),
        200,
    )


@bp.route("/export/<file_name>", methods=["DELETE"])
def export_delete(file_name: str):
    safe_file_name = secure_filename(export_filename_check_extension(file_name))
    file = os.path.join(EXPORT_DIR, safe_file_name)

    if not os.path.exists(file):
        return make_response(
            jsonify({"success": False, "message": f"{file_name} not found."}),
            404,
        )

    os.unlink(file)
    return make_response(
        jsonify(
            {
                "success": True,
                "message": "Successfully deleted file.",
            }
        ),
        200,
    )


def imagestream(detected_frames_processor, camera_name, fps, height, draw_options):
    while True:
        # max out at specified FPS
        time.sleep(1 / fps)
        frame = detected_frames_processor.get_current_frame(camera_name, draw_options)
        if frame is None:
            frame = np.zeros((height, int(height * 16 / 9), 3), np.uint8)

        width = int(height * frame.shape[1] / frame.shape[0])
        frame = cv2.resize(frame, dsize=(width, height), interpolation=cv2.INTER_LINEAR)

        ret, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + jpg.tobytes() + b"\r\n\r\n"
        )


@bp.route("/ffprobe", methods=["GET"])
def ffprobe():
    path_param = request.args.get("paths", "")

    if not path_param:
        return make_response(
            jsonify({"success": False, "message": "Path needs to be provided."}), 404
        )

    if path_param.startswith("camera"):
        camera = path_param[7:]

        if camera not in current_app.frigate_config.cameras.keys():
            return make_response(
                jsonify(
                    {"success": False, "message": f"{camera} is not a valid camera."}
                ),
                404,
            )

        if not current_app.frigate_config.cameras[camera].enabled:
            return make_response(
                jsonify({"success": False, "message": f"{camera} is not enabled."}), 404
            )

        paths = map(
            lambda input: input.path,
            current_app.frigate_config.cameras[camera].ffmpeg.inputs,
        )
    elif "," in clean_camera_user_pass(path_param):
        paths = path_param.split(",")
    else:
        paths = [path_param]

    # user has multiple streams
    output = []

    for path in paths:
        ffprobe = ffprobe_stream(path.strip())
        output.append(
            {
                "return_code": ffprobe.returncode,
                "stderr": (
                    ffprobe.stderr.decode("unicode_escape").strip()
                    if ffprobe.returncode != 0
                    else ""
                ),
                "stdout": (
                    json.loads(ffprobe.stdout.decode("unicode_escape").strip())
                    if ffprobe.returncode == 0
                    else ""
                ),
            }
        )

    return jsonify(output)


@bp.route("/vainfo", methods=["GET"])
def vainfo():
    vainfo = vainfo_hwaccel()
    return jsonify(
        {
            "return_code": vainfo.returncode,
            "stderr": (
                vainfo.stderr.decode("unicode_escape").strip()
                if vainfo.returncode != 0
                else ""
            ),
            "stdout": (
                vainfo.stdout.decode("unicode_escape").strip()
                if vainfo.returncode == 0
                else ""
            ),
        }
    )


@bp.route("/logs/<service>", methods=["GET"])
def logs(service: str):
    log_locations = {
        "frigate": "/dev/shm/logs/frigate/current",
        "go2rtc": "/dev/shm/logs/go2rtc/current",
        "nginx": "/dev/shm/logs/nginx/current",
    }
    service_location = log_locations.get(service)

    if not service_location:
        return make_response(
            jsonify({"success": False, "message": "Not a valid service"}),
            404,
        )

    try:
        file = open(service_location, "r")
        contents = file.read()
        file.close()
        return contents, 200
    except FileNotFoundError as e:
        logger.error(e)
        return make_response(
            jsonify({"success": False, "message": "Could not find log file"}),
            500,
        )


@bp.route("/restart", methods=["POST"])
def restart():
    try:
        restart_frigate()
    except Exception as e:
        logging.error(f"Error restarting Frigate: {e}")
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": "Unable to restart Frigate.",
                }
            ),
            500,
        )

    return make_response(
        jsonify(
            {
                "success": True,
                "message": "Restarting (this can take up to one minute)...",
            }
        ),
        200,
    )
