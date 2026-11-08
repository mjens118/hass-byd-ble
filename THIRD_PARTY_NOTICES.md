# Third-party notices

## BydBleAutoLock

The Bluetooth protocol handling in this integration (session setup, frame format, command codes and
the dkey provisioning flow) is derived from research and code in
[PoorGrammerA/BydBleAutoLock](https://github.com/PoorGrammerA/BydBleAutoLock). `custom_components/byd_ble/codec.py`
is a Python port of its codec, and `custom_components/byd_ble/provisioning.py` is a Python port of its watch
key service (`BydWatchKeyService.java`) and crypto helpers (`CryptoUtils.java`). The known-answer values in the
tests were produced by running that project's Java. That project is released under the MIT License, reproduced
here as its terms require:

```
MIT License

Copyright (c) 2026 BLE AutoLock contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Only the commands that project marks as verified are exposed here. The five commands (lock, unlock,
open boot, flash lights, flash lights and horn) were also confirmed by hand on an AU BYD Sealion 8.

## Not affiliated with BYD

This is an independent, unofficial project. It is not affiliated with, endorsed by, or supported by BYD.
BYD and related names are trademarks of their owners and are used here only to say which vehicles the
integration is meant to work with. The MIT License of this project does not grant any rights to BYD
trademarks, private services, accounts, firmware or third-party applications.
