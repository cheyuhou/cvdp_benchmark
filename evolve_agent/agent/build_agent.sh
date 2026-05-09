#!/bin/sh
# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0
set -eu

docker build -f Dockerfile-base -t cvdp-evolve-agent-base .
docker build -f Dockerfile-agent -t cvdp-evolve-agent --no-cache .
echo "built: cvdp-evolve-agent"
echo "remember to pass QWEN_API_KEY via -e at run time"
