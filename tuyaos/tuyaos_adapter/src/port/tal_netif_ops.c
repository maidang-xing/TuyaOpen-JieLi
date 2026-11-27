/* core_netif_ops_platform for the legacy TuyaOpen Jieli port.
 *
 * tuyaopen-core's core_netif.c dispatches through this ops card; the empty
 * definition the core used to provide is gone, so the port supplies it. The
 * station IP and link state come straight from the TKL WiFi contract. Link
 * changes are reported once at registration: the vendor stack exposes station
 * status but no poll-free event hook this adapter can forward.
 */
#include "core_netif_ops.h"

#include <stddef.h>

#include "tkl_wifi.h"

static OPERATE_RET __netif_get_ip(NW_IP_S *ip)
{
    if (ip == NULL) {
        return OPRT_INVALID_PARM;
    }
    return tkl_wifi_get_ip(WF_STATION, ip);
}

static OPERATE_RET __netif_set_link_cb(CORE_NETIF_LINK_CHANGE_CB cb)
{
    WF_STATION_STAT_E stat;

    if (cb == NULL) {
        return OPRT_INVALID_PARM;
    }
    if (tkl_wifi_station_get_status(&stat) != OPRT_OK) {
        return OPRT_COM_ERROR;
    }
    cb(stat == WSS_GOT_IP ? CORE_NETIF_LINK_UP : CORE_NETIF_LINK_DOWN);
    return OPRT_OK;
}

CORE_NETIF_OPS_T core_netif_ops_platform = {
    .get_ip = __netif_get_ip,
    .set_link_cb = __netif_set_link_cb,
    /* No interface-list API in this port: callers fall back to get_ip(). */
    .foreach_addr = NULL,
};
