#!/bin/bash
set -x

function main {
  init_params "$@"
  run_benchmark

}

# init params
function init_params {
  for var in "$@"
  do
    case $var in
      --input_model=*)
          input_model=$(echo "$var" |cut -f2 -d=)
      ;;
      --dataset_location=*)
          dataset_location=$(echo "$var" |cut -f2 -d=)
      ;;
      --label_path=*)
          label_path=$(echo "$var" |cut -f2 -d=)
      ;;
      --mode=*)
          mode=$(echo "$var" |cut -f2 -d=)
      ;;
      --intra_op_num_threads=*)
          intra_op_num_threads=$(echo "$var" |cut -f2 -d=)
      ;;
    esac
  done

}

# run_benchmark
function run_benchmark {

    python main.py \
            --model_path "${input_model}" \
            --dataset_location "${dataset_location}" \
            --label_path "${label_path-${dataset_location}/../val.txt}" \
            --mode "${mode}" \
            --batch_size 1 \
            --intra_op_num_threads "${intra_op_num_threads-4}" \
            --benchmark
            
}

main "$@"
