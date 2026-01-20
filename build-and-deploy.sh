#!/bin/bash

docker stop yt-dlp-streamer
docker rm yt-dlp-streamer
docker image rm yt-dlp-streamer:latest

docker build -t yt-dlp-streamer:latest .
docker compose up -d
docker logs -f yt-dlp-streamer

#docker tag yt-dlp-streamer:latest n7fr846yfa6ohlhe/mbugai:remote-strean-downloader-1
#docker push n7fr846yfa6ohlhe/mbugai:remote-strean-downloader-1