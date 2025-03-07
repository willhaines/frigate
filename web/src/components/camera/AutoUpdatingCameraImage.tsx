import { useCallback, useEffect, useState } from "react";
import CameraImage from "./CameraImage";

type AutoUpdatingCameraImageProps = {
  camera: string;
  searchParams?: {};
  showFps?: boolean;
  className?: string;
  reloadInterval?: number;
};

const MIN_LOAD_TIMEOUT_MS = 200;

export default function AutoUpdatingCameraImage({
  camera,
  searchParams = "",
  showFps = true,
  className,
  reloadInterval = MIN_LOAD_TIMEOUT_MS,
}: AutoUpdatingCameraImageProps) {
  const [key, setKey] = useState(Date.now());
  const [fps, setFps] = useState<string>("0");
  const [timeoutId, setTimeoutId] = useState<NodeJS.Timeout>();

  useEffect(() => {
    if (reloadInterval == -1) {
      return;
    }

    setKey(Date.now());

    return () => {
      if (timeoutId) {
        clearTimeout(timeoutId);
        setTimeoutId(undefined);
      }
    };
  }, [reloadInterval]);

  const handleLoad = useCallback(() => {
    if (reloadInterval == -1) {
      return;
    }

    const loadTime = Date.now() - key;

    if (showFps) {
      setFps((1000 / Math.max(loadTime, reloadInterval)).toFixed(1));
    }

    setTimeoutId(
      setTimeout(
        () => {
          setKey(Date.now());
        },
        loadTime > reloadInterval ? 1 : reloadInterval
      )
    );
  }, [key, setFps]);

  return (
    <div className={className}>
      <CameraImage
        camera={camera}
        onload={handleLoad}
        searchParams={`cache=${key}&${searchParams}`}
      />
      {showFps ? <span className="text-xs">Displaying at {fps}fps</span> : null}
    </div>
  );
}
