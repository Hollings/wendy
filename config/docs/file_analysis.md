# File Analysis (analyze_file endpoint)

```bash
curl -X POST "http://localhost:8945/api/analyze_file" \
  -F "file=@/path/to/file.jpg" \
  -F "prompt=Describe this file in full detail, 5-10 sentences."
```

## Supported formats

- Images: PNG, JPEG, WEBP, HEIC, HEIF
- Audio: WAV, MP3, AIFF, AAC, OGG, FLAC
- Video: MP4, MPEG, MOV, AVI, WEBM, WMV

## Limits

- Max file size: 20MB
- Max video duration: 5 minutes
- Max audio duration: 30 minutes

## Prompt tips

- Default: "Describe this file in full detail, 5-10 sentences. Include all visible text, objects, people, colors, and context."
- Specific: "What text is visible in this image?" / "Is there a dog in this photo?"
- Audio: "Describe this audio in detail - the mood, genre, instruments, tempo, and any vocals or speech."
- Video: "Summarize everything that happens in this video, scene by scene."

## Images: use both Read and analyze_file

For images, call the Read tool first (your own view), then analyze_file (better at details, text, faces, specific objects). Trust analyze_file more for specifics. Never say "Gemini said" or "according to the analysis" - these are your own tools.
