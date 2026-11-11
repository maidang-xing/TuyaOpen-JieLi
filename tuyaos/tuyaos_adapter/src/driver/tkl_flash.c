#include "tkl_flash.h"
#include "tuya_error_code.h"
#include "asm/sfc_norflash_api.h"
#include "device/ioctl_cmds.h"

#include <string.h>

/*
 * The Tuya data partitions live in the vendor flash map declared by
 * cpu/<chip>/tools/isd_config_rule.c. tools/jieli_build/board_config.py reads
 * the hex literals below and declares their span as a fixed, OPT=1 reserved
 * area, so the vendor downloader no longer erases it on every USB download.
 * Keep the format: NAME_START / NAME_SIZE, one plain hex literal per line.
 */
#if defined(JIELI_BOARD_FLASH_MIB)
#define JIELI_FLASH_SIZE       ((uint32_t)JIELI_BOARD_FLASH_MIB * 1024U * 1024U)
#else
#define JIELI_FLASH_SIZE       (8U * 1024U * 1024U)
#endif
#define JIELI_FLASH_BLOCK      0x00001000U
#define JIELI_KV_KEY_START     0x00300000U
#define JIELI_KV_KEY_SIZE      0x00001000U
#define JIELI_KV_DATA_START    0x00301000U
#define JIELI_KV_DATA_SIZE     0x00010000U
#define JIELI_UF_START         0x00311000U
#define JIELI_UF_SIZE          0x00080000U
#define JIELI_RCD_START        0x00391000U
#define JIELI_RCD_SIZE         0x00030000U
/* The generic user partition takes the last 64 KiB of the RCD area, so the
 * TuyaOpen span keeps its original end and the vendor's AUTO-placed BTIF/USER
 * areas do not move. Nothing in the tree consumes TUYA_FLASH_TYPE_RCD. */
#define JIELI_USER0_START      0x003C1000U
#define JIELI_USER0_SIZE       0x00010000U

static OPERATE_RET jieli_flash_check_range(uint32_t addr, uint32_t size)
{
    if (addr >= JIELI_FLASH_SIZE || size > JIELI_FLASH_SIZE - addr) {
        return OPRT_INVALID_PARM;
    }
    return OPRT_OK;
}

/* The vendor documents norflash_open() as the first call of any user of the
 * flash API. It is idempotent, and the device it opens is what the NULL-handle
 * read/write/ioctl calls below resolve against. */
static void jieli_flash_open_once(void)
{
    static uint8_t opened;

    if (!opened) {
        norflash_open(NULL, NULL, NULL);
        opened = 1u;
    }
}

/* Only the partitions this adapter owns may be written or erased. Without this
 * an application could hand tkl_flash_erase() an address inside the firmware
 * image, which is a bricked board rather than an error code. */
static int jieli_flash_is_owned(uint32_t addr, uint32_t size)
{
    static const struct {
        uint32_t start;
        uint32_t size;
    } partitions[] = {
        {JIELI_KV_KEY_START, JIELI_KV_KEY_SIZE},
        {JIELI_KV_DATA_START, JIELI_KV_DATA_SIZE},
        {JIELI_UF_START, JIELI_UF_SIZE},
        {JIELI_RCD_START, JIELI_RCD_SIZE},
        {JIELI_USER0_START, JIELI_USER0_SIZE},
    };
    unsigned int i;

    for (i = 0u; i < sizeof(partitions) / sizeof(partitions[0]); i++) {
        if (addr >= partitions[i].start &&
            size <= partitions[i].size &&
            size <= partitions[i].start + partitions[i].size - addr) {
            return 1;
        }
    }
    return 0;
}

OPERATE_RET tkl_flash_read(uint32_t addr, uint8_t *dst, uint32_t size)
{
    if (NULL == dst || OPRT_OK != jieli_flash_check_range(addr, size)) {
        return OPRT_INVALID_PARM;
    }
    jieli_flash_open_once();
    return (norflash_read(NULL, dst, size, addr) == (int)size) ? OPRT_OK : OPRT_COM_ERROR;
}

OPERATE_RET tkl_flash_write(uint32_t addr, const uint8_t *src, uint32_t size)
{
    if (NULL == src || OPRT_OK != jieli_flash_check_range(addr, size)) {
        return OPRT_INVALID_PARM;
    }
    if (!jieli_flash_is_owned(addr, size)) {
        return OPRT_INVALID_PARM;
    }
    jieli_flash_open_once();
    /* The downloader leaves the flash write-protected; the vendor's own flash
     * example releases it before every write, so do the same here. */
    norflash_ioctl(NULL, IOCTL_SET_WRITE_PROTECT, 0);
    return (norflash_write(NULL, (void *)src, size, addr) == (int)size) ? OPRT_OK : OPRT_COM_ERROR;
}

OPERATE_RET tkl_flash_erase(uint32_t addr, uint32_t size)
{
    uint32_t offset;

    if (0U == size || (addr % JIELI_FLASH_BLOCK) != 0U || (size % JIELI_FLASH_BLOCK) != 0U ||
        OPRT_OK != jieli_flash_check_range(addr, size) || !jieli_flash_is_owned(addr, size)) {
        return OPRT_INVALID_PARM;
    }
    jieli_flash_open_once();
    /* Erase a sector at a time through the norflash ioctl the vendor's own
     * flash example uses. sfc_erase_zone() belongs to the VM path and stalls
     * the chip when it is handed an address outside that area. */
    norflash_ioctl(NULL, IOCTL_SET_WRITE_PROTECT, 0);
    for (offset = 0u; offset < size; offset += JIELI_FLASH_BLOCK) {
        if (norflash_ioctl(NULL, IOCTL_ERASE_SECTOR, addr + offset) < 0) {
            return OPRT_COM_ERROR;
        }
    }
    return OPRT_OK;
}

OPERATE_RET tkl_flash_lock(uint32_t addr, uint32_t size)
{
    (void)addr;
    (void)size;
    return OPRT_OK;
}

OPERATE_RET tkl_flash_unlock(uint32_t addr, uint32_t size)
{
    (void)addr;
    (void)size;
    return OPRT_OK;
}

OPERATE_RET tkl_flash_get_one_type_info(TUYA_FLASH_TYPE_E type, TUYA_FLASH_BASE_INFO_T *info)
{
    if (NULL == info) {
        return OPRT_INVALID_PARM;
    }
    memset(info, 0, sizeof(*info));
    info->partition_num = 1;
    info->partition[0].block_size = JIELI_FLASH_BLOCK;

    switch (type) {
    case TUYA_FLASH_TYPE_KV_KEY:
        info->partition[0].start_addr = JIELI_KV_KEY_START;
        info->partition[0].size = JIELI_KV_KEY_SIZE;
        break;
    case TUYA_FLASH_TYPE_KV_DATA:
        info->partition[0].start_addr = JIELI_KV_DATA_START;
        info->partition[0].size = JIELI_KV_DATA_SIZE;
        break;
    case TUYA_FLASH_TYPE_UF:
        info->partition[0].start_addr = JIELI_UF_START;
        info->partition[0].size = JIELI_UF_SIZE;
        break;
    case TUYA_FLASH_TYPE_RCD:
        info->partition[0].start_addr = JIELI_RCD_START;
        info->partition[0].size = JIELI_RCD_SIZE;
        break;
    case TUYA_FLASH_TYPE_USER0:
        info->partition[0].start_addr = JIELI_USER0_START;
        info->partition[0].size = JIELI_USER0_SIZE;
        break;
    default:
        memset(info, 0, sizeof(*info));
        return OPRT_NOT_SUPPORTED;
    }
    return OPRT_OK;
}
