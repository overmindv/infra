#!/usr/bin/env sh

SERVICES="frontend api-gateway users entities tasks task-hunter"
REGISTRY="${IMAGE_REGISTRY:-overmindv}"
LOCAL_TAG="${IMAGE_TAG:-local}"
LATEST_TAG="${IMAGE_TAG:-latest}"
