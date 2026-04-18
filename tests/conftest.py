import sys
import os

# Add system gps module path for CI environments where setup-python
# creates a virtualenv that cannot see system packages.
# Only insert the path if the gps module is not already importable.
try:
    import gps
except ImportError:
    # Find and add only the gps module, not all of dist-packages
    _gps_candidates = [
        '/usr/lib/python3/dist-packages',
        '/usr/lib/python3.11/dist-packages',
        '/usr/lib/python3.12/dist-packages',
        '/usr/lib/python3.13/dist-packages',
    ]
    for _path in _gps_candidates:
        _gps_init = os.path.join(_path, 'gps', '__init__.py')
        if os.path.exists(_gps_init):
            # Only add this path if it won't shadow a better numpy
            sys.path.insert(0, _path)
            break
