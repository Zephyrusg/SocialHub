# Tiny pre-generated test media

- `portrait.jpg`: synthetic blue 800×1000 JPEG, 13,229 bytes.
- `portrait.png`: synthetic blue 800×1000 PNG, approximately 4.5 KiB.
- `reel-h264-3s.mp4`: synthetic black 720×1280 H.264/yuv420p MP4, three seconds, no audio, 5,622 bytes.

These files contain no user media. They were generated once during development; normal tests copy these fixtures and run metadata validation only. No encoder or transcoding dependency is needed in runtime or test images. Keep fixtures small, deterministic in meaning and safe to commit. Video tests use the pinned prebuilt static ffprobe copied into both images.
