# TODO

## More source types
Sources live in `sources.py`: write a class with `matches(url)` / `resolve(url)`
returning a `Resolved`, and decorate it with `@register`.

- [ ] RSS feed URLs (latest episode by default; `--episode N` / title match to choose)
- [ ] Direct audio URLs (`.mp3` / `.m4a`) — may need only a trivial `Resolved`
- [ ] Spotify links — audio is DRM-protected and can't be downloaded; best effort is
      finding the show's RSS feed and matching the episode by title (needs the RSS source above)
- [ ] Other podcast hosts (Overcast, Pocket Casts, Google/YouTube Music share links)
- [ ] Publisher transcripts: check the feed's `<podcast:transcript>` tag before running Whisper

## Performance
- [ ] Benchmark Whisper models on podcast-length audio (tiny / base / small / turbo):
      wall time vs. accuracy, on this machine's CPU. One data point so far: `base`
      took ~50 s for a ~12 min episode (Up First, 2,733 words).
- [ ] Decide whether to keep `base` as the podcast default based on that benchmark
- [ ] Consider GPU/Metal acceleration or `distil-large-v3` if CPU is too slow for 1 h+ episodes

## Other
- [ ] Test `--timestamps`, `--lang` other than `en`, and manual-vs-auto captions on YouTube
- [ ] Progress output for long transcriptions
