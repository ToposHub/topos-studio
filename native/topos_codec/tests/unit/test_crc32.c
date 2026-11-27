/* CRC-32 IEEE（spec §2.6/A.6）：已知向量 + 位级参考实现交叉验证 + 增量一致性 */
#include "common/crc32.h"

#include <string.h>

#include "mini_test.h"

static uint32_t crc_ref_byte(uint32_t crc, uint8_t b)
{
    crc ^= b;
    for (int i = 0; i < 8; ++i) {
        crc = (crc & 1u) != 0u ? (crc >> 1) ^ 0xEDB88320u : crc >> 1;
    }
    return crc;
}

static uint32_t crc_ref(uint32_t seed, const void* data, size_t size)
{
    const uint8_t* p = (const uint8_t*)data;
    uint32_t crc = seed ^ 0xFFFFFFFFu;
    for (size_t i = 0; i < size; ++i) { crc = crc_ref_byte(crc, p[i]); }
    return crc ^ 0xFFFFFFFFu;
}

int main(void)
{
    /* 规范自测向量（强制） */
    MT_CHECK_EQ_U64(tc_crc32("123456789", 9), 0xCBF43926ull);
    MT_CHECK_EQ_U64(crc_ref(0u, "123456789", 9), 0xCBF43926ull);

    /* 其他标准向量（RFC 1952 / 常见校验集） */
    MT_CHECK_EQ_U64(tc_crc32("", 0), 0x00000000ull);
    MT_CHECK_EQ_U64(tc_crc32("a", 1), 0xE8B7BE43ull);
    MT_CHECK_EQ_U64(tc_crc32("abc", 3), 0x352441C2ull);
    MT_CHECK_EQ_U64(tc_crc32("message digest", 14), 0x20159D7Full);
    MT_CHECK_EQ_U64(tc_crc32("abcdefghijklmnopqrstuvwxyz", 26), 0x4C2750BDull);

    /* 随机数据：表实现 == 位级参考（长度含 0 与非对齐长度） */
    uint8_t buf[600];
    for (int iter = 0; iter < 200; ++iter) {
        size_t n = (size_t)(mt_rand_u64() % 601ull);
        for (size_t i = 0; i < n; ++i) { buf[i] = (uint8_t)(mt_rand_u64() >> 24); }
        uint32_t a = tc_crc32(buf, n);
        uint32_t b = crc_ref(0u, buf, n);
        if (a != b) {
            mt_report(__FILE__, __LINE__, "random cross-check mismatch");
            break;
        }
        /* 增量 == 一次性 */
        uint32_t inc = 0;
        size_t half = n / 2;
        inc = tc_crc32_update(inc, buf, half);
        inc = tc_crc32_update(inc, buf + half, n - half);
        if (inc != a) {
            mt_report(__FILE__, __LINE__, "incremental mismatch");
            break;
        }
    }

    /* Large, unaligned buffers and an incremental stream crossing 1 KiB. */
    uint8_t large[8192 + 16];
    for (size_t i = 0; i < sizeof(large); ++i) { large[i] = (uint8_t)mt_rand_u64(); }
    const size_t lengths[] = { 1023u, 1024u, 1025u, 4097u, 8192u };
    for (size_t offset = 0; offset < 16u; ++offset) {
        const uint8_t* data = large + offset;
        for (size_t i = 0; i < sizeof(lengths) / sizeof(lengths[0]); ++i) {
            const size_t length = lengths[i];
            const uint32_t seed = (uint32_t)mt_rand_u64();
            const uint32_t expected = crc_ref(seed, data, length);
            MT_CHECK_EQ_U64(tc_crc32_update(seed, data, length), expected);
            const uint32_t first = tc_crc32_update(seed, data, 1u);
            MT_CHECK_EQ_U64(tc_crc32_update(first, data + 1u, length - 1u), expected);
        }
    }

    return MT_MAIN_RETURN();
}
