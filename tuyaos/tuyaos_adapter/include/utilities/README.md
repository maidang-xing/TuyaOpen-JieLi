# Vendored TuyaOpen utilities

This directory is a **copy** of `<TuyaOpen>/tools/porting/adapter/utilities`,
kept inside the JIELI platform so that changes JIELI needs do not have to be
made in the shared TuyaOpen tree.

It follows the same convention as the other platforms, which each vendor their
own copy under `tuyaos/tuyaos_adapter/include/utilities/`.

## What is used from here

| consumer | path |
|---|---|
| adapter CMake build (`libtuyaos.a`) | `platform_config.cmake` and `tuyaos/tuyaos_adapter/CMakeLists.txt` add `include/utilities/include` |
| staged vendor build | `tools/jieli_build/sdk_overlay.py` copies this directory into the staging tree as `tuya_utilities/` and adds `include/utilities/include` to the vendor Makefile |

Nothing in the JIELI build reads the shared `tools/porting/adapter/utilities`
any more. Verify with:

```bash
grep -rn "porting/adapter/utilities" platform/JIELI   # expect no hits
```

## Local change

`include/tuya_cloud_types.h` widens `TUYA_GPIO_NUM_E` from 64 to 128 entries
(`TUYA_GPIO_NUM_64` .. `TUYA_GPIO_NUM_127`). JIELI chips have up to 128 GPIOs -
ports A..H on wl82, and A..F plus the sparse USB pairs on wl83 - while the
shared header stopped at port D, which made PE11 (the AC792N console RX, pin 75)
unreachable through `tkl_gpio_*`.

The matching platform-side bound is `TKL_GPIO_PIN_MAX` in
`tuyaos_adapter/src/driver/tkl_gpio.c`; it must stay >= this enum's MAX.
`jieli_pin_exists()` in that same file still refuses pins a given chip does not
populate (wl83 has no PC14/PC15 and no PF6..PF15; wl82 has no port I register
block), and `tkl_pinmux.c` declares it `extern` so the table has one definition.

## Refreshing from upstream

Re-copy the tree and re-apply the enum extension, or diff first:

```bash
diff -r <TuyaOpen>/tools/porting/adapter/utilities platform/JIELI/tuyaos/tuyaos_adapter/include/utilities
```

Do not refresh blindly: the 128-entry enum is a deliberate local divergence, and
replacing `tuya_cloud_types.h` without re-applying it silently narrows the GPIO
range back to 64.
