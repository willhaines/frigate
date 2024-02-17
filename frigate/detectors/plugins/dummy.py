from frigate.detectors.detection_api import DetectionApi
from frigate.detectors.detector_config import BaseDetectorConfig

class DummyDetector(DetectionApi):

    def __init__(self, detector_config: BaseDetectorConfig):
        pass

    def detect_raw(self, tensor_input):
        return [
            np.float32([
                0,  # class = person
                1,  # confidence = 100%
                0,  # y_min = 0
                0,  # x_min = 0
                1,  # y_max = 1
                1,  # y_max = 1
            ])
        ]