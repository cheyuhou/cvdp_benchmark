#!/bin/bash

# Auto-generated script to run harness Docker container
# Usage: run_docker_harness_07-new-tb.sh [-d] (where -d enables debug mode with bash entrypoint)
set -e

# Parse command line arguments
DEBUG_MODE=false
while getopts 'd' flag; do
  case "${flag}" in
    d) DEBUG_MODE=true ;;
  esac
done

# Use shared bridge network: cvdp-bridge-subset-a364bd4f
NETWORK_CREATED=0

# Check if network exists, create if needed
if ! docker network inspect cvdp-bridge-subset-a364bd4f &>/dev/null; then
  echo "Creating Docker network cvdp-bridge-subset-a364bd4f..."
  docker network create --driver bridge cvdp-bridge-subset-a364bd4f
  NETWORK_CREATED=1
fi

# Function to clean up resources
cleanup() {
  echo "Cleaning up Docker resources..."
  docker compose -f /workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/docker-compose.yml -p cvdp_copilot_carry_lookahead_adder_1_1778039498 kill 07-new-tb 2>/dev/null || true
  docker rmi cvdp_copilot_carry_lookahead_adder_1_1778039498-07-new-tb 2>/dev/null || true
  if [ $NETWORK_CREATED -eq 1 ]; then
    echo "Removing Docker network cvdp-bridge-subset-a364bd4f..."
    docker network rm cvdp-bridge-subset-a364bd4f 2>/dev/null || true
  fi
}

# Set up cleanup trap
trap cleanup EXIT

# Run the harness container
echo "Running harness with project name: cvdp_copilot_carry_lookahead_adder_1_1778039498"
# Get current user and group IDs
USER_ID=$(id -u)
GROUP_ID=$(id -g)

if [ "$DEBUG_MODE" = true ]; then
  echo "DEBUG MODE: Starting container with bash entrypoint"
  docker compose -f /workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/docker-compose.yml -p cvdp_copilot_carry_lookahead_adder_1_1778039498 run --rm --user $USER_ID:$GROUP_ID -e HOME=/code/rundir --entrypoint bash -v "/workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/docs:/code/docs" -v "/workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/rundir:/code/rundir" -v "/workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/rtl:/code/rtl" -v "/workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/verif:/code/verif" -v "/workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/src:/code/src" -v "/workspace/cvdp_benchmark/src/llm_lib:/pysubj" --rm -w /code/rundir --env OPENAI_USER_KEY=OPENROUTER_KEY_PLACEHOLDER 07-new-tb
else
  docker compose -f /workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/docker-compose.yml -p cvdp_copilot_carry_lookahead_adder_1_1778039498 run --rm --user $USER_ID:$GROUP_ID -e HOME=/code/rundir -v "/workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/docs:/code/docs" -v "/workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/rundir:/code/rundir" -v "/workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/rtl:/code/rtl" -v "/workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/verif:/code/verif" -v "/workspace/cvdp_benchmark/work_evolve_v8_cid003/iter_0_sample_1/cvdp_copilot_Carry_Lookahead_Adder/harness/1/src:/code/src" -v "/workspace/cvdp_benchmark/src/llm_lib:/pysubj" --rm -w /code/rundir --env OPENAI_USER_KEY=OPENROUTER_KEY_PLACEHOLDER 07-new-tb
fi
exit_code=$?

# Exit with the same code as the docker command
exit $exit_code
