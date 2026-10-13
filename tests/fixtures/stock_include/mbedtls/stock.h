/* A stand-in for TuyaOpen's mbedtls-3.1.0 headers, used by
 * tests/test_provider_map.py to pin the stock/non-stock split without reading
 * the main repository's src/libtls tree.
 *
 * The names listed here are the ones the audit fixture expects to be *stock*:
 * upstream mbedTLS declares them, so a vendor object providing one is a
 * conflict. The JieLi extensions the fixture uses are deliberately absent, so
 * the audit must report them as extensions instead of conflicts.
 */
#ifndef TEST_STOCK_MBEDTLS_H
#define TEST_STOCK_MBEDTLS_H

int mbedtls_aes_init(void);
int mbedtls_ctr_drbg_seed(void);
int mbedtls_net_connect(void);
int mbedtls_net_free(void);
int mbedtls_net_init(void);
int mbedtls_net_recv(void);
int mbedtls_net_send(void);
int mbedtls_ssl_write(void);
int mbedtls_ssl_read(void);

#endif /* TEST_STOCK_MBEDTLS_H */
