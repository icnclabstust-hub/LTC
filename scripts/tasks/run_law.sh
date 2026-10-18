#!/bin/bash
cd /mnt/f/LTC-BE
export PATH="$HOME/.local/bin:$PATH"
export PYTHONPATH="/mnt/f/LTC-BE/src"
python3 -m ingestion.run --source=law
