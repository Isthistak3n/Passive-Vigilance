# KismetModule Improvements

## Changes
- Move synchronous `iw dev` call to `run_in_executor()` for better async hygiene
- Improve distinction between transient polling errors and fatal connection errors
- Cleaner logging and warning messages

## Goal
Improve reliability and prevent event loop blocking during long-running operation on Raspberry Pi.

Part of the ongoing code review refactor series.