#!/bin/bash

docker stop yt-dlp-streamer
docker rm yt-dlp-streamer
docker image rm yt-dlp-streamer:latest

#docker build -t yt-dlp-streamer:latest .
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t yt-dlp-streamer:latest .

#docker tag yt-dlp-streamer:latest n7fr846yfa6ohlhe/mbugai:remote-stream-downloader-39
#docker push n7fr846yfa6ohlhe/mbugai:remote-stream-downloader-39

docker compose up -d
docker logs -f yt-dlp-streamer
