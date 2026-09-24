# Sample data

The system is tested on three short lectures about **clearly different subjects**: computer science, economics and biology. Because the subjects differ, you can see that retrieval stays within the right video and that the per-topic report means something.

| File | Source | Publisher | Length |
|---|---|---|---|
| `recursion_mit.mp4` | [1.10.7 Recursive Functions](https://www.youtube.com/watch?v=tOsdeaYDCMk) | MIT OpenCourseWare | 14:03 |
| `inflation_khan.mp4` | [Introduction to inflation](https://www.youtube.com/watch?v=AaR1mPrdbTc) | Khan Academy | 7:32 |
| `photosynthesis_khan.mp4` | [Photosynthesis](https://www.youtube.com/watch?v=-rsYk4eCKnA) | Khan Academy | 13:37 |

## How to get them

The videos are **not committed** to the repo. Download them with:

```bash
python scripts/fetch_sample_videos.py
```

This saves small 360p MP4 files into `sample_data/videos/`. Any other video or audio file dropped into that folder works too. Supported formats: `.mp4 .mkv .webm .mov .avi .m4a .mp3 .wav`.

## Licensing

- YouTube does not show a Creative Commons licence on these three uploads.
- MIT OpenCourseWare and Khan Academy both publish their course content under **CC BY-NC-SA** on their own sites. They're used here for non-commercial, educational assessment purposes, which the brief allows ("any publicly available educational videos").
- The videos are not redistributed with this repository.

## Generated outputs

Real outputs produced by running `scripts/demo.py` on these videos are saved in [`docs/sample-outputs/`](../docs/sample-outputs/): the assessment, the answer evaluations and the learning reports, in both JSON and Markdown.
