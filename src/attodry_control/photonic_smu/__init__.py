"""Pinned Three-SMU implementation reused without changes from the photonics branch.

This internal package isolates its newer active-role/point-session contracts from
legacy independent Three-SMU CLI/config APIs. No lock-in or cryostat code is copied.
Updates must keep these three source files together and rerun the reference tests.
"""
SOURCE_COMMIT = '016d741b587bb7e2d39d6bcf13dbc51eece8fab1'
SOURCE_SHA256 = {'three_smu.py': '00b949884b214ccb89e71e92476c02fad5a8f229fb829b199a81a617ce6ef477', 'three_smu_config.py': '8d4d2edd8bfb7487f5345511e48439ec72e6a90f1337c836a64c20827d73d5ef', 'keithley2400.py': 'dd681f1838244afced9d28b9a00d60972e50267e61d3fc7d1c04fc53bd1f22a3'}
