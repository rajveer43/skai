#!/bin/bash
#
# This script will run all Python unit tests in SKAI. You should be able to run
# this script on Linux as long as you have a recent Python version (>=3.7) and
# virtualenv installed.

set -e
set -x

if [ -n "$KOKORO_ROOT" ]
then
  # Setup for invoking from continuous integration test framework.
  VIRTUALENV_PATH="${KOKORO_ROOT}/skai_env"
  SKAI_DIR="${KOKORO_ARTIFACTS_DIR}/github/skai"

  PY_VERSION=3.11.4
  pyenv uninstall -f $PY_VERSION
  # Have to install lzma library first, otherwise will get an error
  # "ModuleNotFoundError: No module named '_lzma'".
  sudo apt-get install liblzma-dev
  pyenv install -f $PY_VERSION
  pyenv global $PY_VERSION
else
  # Setup for manually triggered runs.
  VIRTUALENV_PATH=/tmp/skai_env
  SKAI_DIR=`dirname $0`/..
fi

function setup {
  if ! which python && which python3
  then
    PYTHON=python3
  else
    PYTHON=python
  fi

  which $PYTHON
  $PYTHON --version
  $PYTHON -m venv "${VIRTUALENV_PATH}"
  source "${VIRTUALENV_PATH}/bin/activate"
  pushd "${SKAI_DIR}"
  which pip
  pip --version
  pip install -r requirements.txt
}

function teardown {
  popd
  deactivate
  rm -rf "${VIRTUALENV_PATH}"
}

function run_tests {
  pushd src
  export PYTHONPATH=.:${PYTHONPATH}
  for test in `find skai -name '*_test.py'`
  do
    python "${test}" || exit 1
  done
}

setup
run_tests
teardown
