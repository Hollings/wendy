# Reading Tweets / X Posts

X/Twitter blocks direct access (Cloudflare). Use the fxtwitter API instead:

```bash
# Get tweet JSON (text, media URLs, author, engagement stats)
curl -s "https://api.fxtwitter.com/{username}/status/{tweet_id}" -A "Mozilla/5.0"

# Extract with python:
curl -s "https://api.fxtwitter.com/{user}/status/{id}" -A "Mozilla/5.0" | python3 -c "
import sys, json; d = json.load(sys.stdin)['tweet']
print(d['text'])
for p in d.get('media',{}).get('photos',[]): print(p['url'])
"
```

This gives full tweet text, image URLs, author info, and engagement. Download images separately to view them.

## URL Patterns
- `twitter.com/{user}/status/{id}`
- `x.com/{user}/status/{id}`
- `fxtwitter.com/{user}/status/{id}`
- `vxtwitter.com/{user}/status/{id}`

## Tips
- Always use `-A "Mozilla/5.0"` user agent or the API may reject requests
- Image URLs from the API can be downloaded directly with curl/wget
- Video URLs may require additional processing
- The fxtwitter API is third-party and free - no auth needed
