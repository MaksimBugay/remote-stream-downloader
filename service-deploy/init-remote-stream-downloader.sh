#!/bin/bash

docker service rm pushca-backend_remote-stream-downloader

#docker service rm pushca-backend_vpn

(DEPLOY_VERSION=6 docker stack deploy --with-registry-auth -c backend.yml pushca-backend)

sleep 15s

#docker service update --force pushca-backend_main-gateway

docker service logs -f pushca-backend_remote-stream-downloader