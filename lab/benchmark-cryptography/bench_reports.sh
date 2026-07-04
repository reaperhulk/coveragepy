#!/bin/bash
S=/tmp/claude-0/-home-user-coveragepy/629652d7-bb14-5436-9d03-584022642139/scratchpad
cd $S/cryptography
DATA=${1:-$S/rel-1.coverage}
for iter in 1 2 3 4 5; do
  for ver in rel branch; do
    BIN=$S/venv-$ver/bin/coverage
    for cmd in report html xml json lcov; do
      rm -rf $S/out-$ver; mkdir -p $S/out-$ver
      case $cmd in
        report) args="report" ;;
        html)   args="html -d $S/out-$ver/htmlcov" ;;
        xml)    args="xml -o $S/out-$ver/coverage.xml" ;;
        json)   args="json -o $S/out-$ver/coverage.json" ;;
        lcov)   args="lcov -o $S/out-$ver/coverage.lcov" ;;
      esac
      start=$(date +%s.%N)
      $BIN $args --data-file=$DATA --keep-combined > /dev/null 2>&1
      rc=$?
      end=$(date +%s.%N)
      echo "$ver,$cmd,$iter,$(echo "$end - $start" | bc),$rc"
    done
  done
done
