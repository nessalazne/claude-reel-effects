# Models

`scripts/setup.sh` downloads these two files here. They are not part of this repo.

| File | What it does | Source | Licence |
|---|---|---|---|
| `selfie_multiclass_256x256.tflite` | Cuts the person out of each frame | [MediaPipe Image Segmenter](https://ai.google.dev/edge/mediapipe/solutions/vision/image_segmenter) | Apache 2.0 |
| `blaze_face_short_range.tflite` | Finds the face so effects can follow it | [MediaPipe Face Detector](https://ai.google.dev/edge/mediapipe/solutions/vision/face_detector) | Apache 2.0 |

Both are from Google's MediaPipe model storage (storage.googleapis.com/mediapipe-models).
