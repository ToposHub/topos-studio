#include "deblock_avx2.h"

#if defined(__x86_64__) || defined(_M_X64)

#include <immintrin.h>

#if defined(_MSC_VER)
#define DB_AVX2
#else
#define DB_AVX2 __attribute__((target("avx2")))
#endif

typedef struct db_vec_gate {
    __m256i alpha;
    __m256i beta;
    __m256i clip;
    __m256i maxv;
} db_vec_gate;

static inline DB_AVX2 __m256i eligible(__m256i p1, __m256i p0, __m256i q0,
                                      __m256i q1, __m256i step, __m256i samples,
                                      const db_vec_gate* g)
{
    const __m256i delta_or_clamp = _mm256_or_si256(
        _mm256_cmpgt_epi16(step, _mm256_set1_epi16(1)),
        _mm256_cmpgt_epi16(samples, g->maxv));
    return _mm256_and_si256(
        _mm256_and_si256(delta_or_clamp, _mm256_cmpgt_epi16(g->alpha, step)),
        _mm256_and_si256(
            _mm256_cmpgt_epi16(g->beta, _mm256_abs_epi16(_mm256_sub_epi16(p1, p0))),
            _mm256_cmpgt_epi16(g->beta, _mm256_abs_epi16(_mm256_sub_epi16(q1, q0)))));
}

static inline DB_AVX2 __m256i correction(__m256i p1, __m256i q1, __m256i delta,
                                        __m256i step, __m256i clip)
{
    const __m256i zero = _mm256_setzero_si256();
    const __m256i half = _mm256_sign_epi16(_mm256_srli_epi16(step, 1), delta);
    /* (12*d + 4*(p1-q1) + 8) >> 4 equals (3*d + p1-q1 + 2) >> 2.
     * Only eligible lanes are written. Their |d| < alpha and |p1-q1|
     * < alpha + 2*beta, so all weighted arithmetic fits signed 16 bits. */
    const __m256i weighted = _mm256_add_epi16(
        _mm256_mullo_epi16(delta, _mm256_set1_epi16(3)), _mm256_sub_epi16(p1, q1));
    const __m256i d = _mm256_srai_epi16(_mm256_add_epi16(weighted, _mm256_set1_epi16(2)), 2);
    const __m256i lo = _mm256_max_epi16(_mm256_min_epi16(half, zero), _mm256_sub_epi16(zero, clip));
    const __m256i hi = _mm256_min_epi16(_mm256_max_epi16(half, zero), clip);
    return _mm256_max_epi16(lo, _mm256_min_epi16(d, hi));
}

static inline DB_AVX2 __m256i clamp_samples(__m256i p, __m256i maxv)
{
    return _mm256_min_epi16(maxv, _mm256_max_epi16(_mm256_setzero_si256(), p));
}

static inline DB_AVX2 db_vec_gate gate_constants(int32_t alpha, int32_t beta,
                                                int32_t clip, int32_t maxv)
{
    const db_vec_gate gate = {
        _mm256_set1_epi16((int16_t)alpha), _mm256_set1_epi16((int16_t)beta),
        _mm256_set1_epi16((int16_t)clip), _mm256_set1_epi16((int16_t)maxv),
    };
    return gate;
}

DB_AVX2 uint32_t tc_deblock_horizontal_row_avx2(uint16_t* p1, uint16_t* p0,
                                               uint16_t* q0, uint16_t* q1, uint32_t width,
                                               int32_t alpha, int32_t beta, int32_t clip,
                                               int32_t maxv)
{
    const db_vec_gate gate = gate_constants(alpha, beta, clip, maxv);
    const __m256i high_bits = _mm256_set1_epi16((int16_t)-16384);
    uint32_t x = 0u;
    for (; x + 16u <= width; x += 16u) {
        const __m256i a = _mm256_loadu_si256((const __m256i*)(p1 + x));
        const __m256i b = _mm256_loadu_si256((const __m256i*)(p0 + x));
        const __m256i c = _mm256_loadu_si256((const __m256i*)(q0 + x));
        const __m256i e = _mm256_loadu_si256((const __m256i*)(q1 + x));
        const __m256i samples = _mm256_or_si256(_mm256_or_si256(a, b), _mm256_or_si256(c, e));
        /* Preserve scalar behavior for arbitrary 16-bit input samples. */
        if (!_mm256_testz_si256(samples, high_bits)) { break; }
        const __m256i delta = _mm256_sub_epi16(c, b);
        const __m256i step = _mm256_abs_epi16(delta);
        const __m256i mask = eligible(a, b, c, e, step, samples, &gate);
        if (_mm256_movemask_epi8(mask) == 0) { continue; }
        const __m256i d = correction(a, e, delta, step, gate.clip);
        const __m256i ds = _mm256_srai_epi16(d, 1);
        const __m256i ap = clamp_samples(_mm256_add_epi16(a, ds), gate.maxv);
        const __m256i bp = clamp_samples(_mm256_add_epi16(b, d), gate.maxv);
        const __m256i cp = clamp_samples(_mm256_sub_epi16(c, d), gate.maxv);
        const __m256i ep = clamp_samples(_mm256_sub_epi16(e, ds), gate.maxv);
        _mm256_storeu_si256((__m256i*)(p1 + x), _mm256_blendv_epi8(a, ap, mask));
        _mm256_storeu_si256((__m256i*)(p0 + x), _mm256_blendv_epi8(b, bp, mask));
        _mm256_storeu_si256((__m256i*)(q0 + x), _mm256_blendv_epi8(c, cp, mask));
        _mm256_storeu_si256((__m256i*)(q1 + x), _mm256_blendv_epi8(e, ep, mask));
    }
    return x;
}

DB_AVX2 uint32_t tc_deblock_vertical_row_avx2(uint16_t* row, uint32_t width,
                                             int32_t alpha, int32_t beta, int32_t clip,
                                             int32_t maxv)
{
    const db_vec_gate gate = gate_constants(alpha, beta, clip, maxv);
    const __m256i high_bits = _mm256_set1_epi16((int16_t)-16384);
    uint32_t x = 8u;
    const __m256i p1_index = _mm256_setr_epi8(
        0,1,0,1,0,1,0,1,8,9,8,9,8,9,8,9, 0,1,0,1,0,1,0,1,8,9,8,9,8,9,8,9);
    const __m256i p0_index = _mm256_add_epi8(p1_index, _mm256_set1_epi8(2));
    const __m256i q0_index = _mm256_add_epi8(p1_index, _mm256_set1_epi8(4));
    const __m256i q1_index = _mm256_add_epi8(p1_index, _mm256_set1_epi8(6));
    /* Combine the low 64 bits of each 128-bit half to process four edges. */
    for (; x + 29u < width; x += 32u) {
        uint16_t* const edge = row + x - 2u;
        const __m256i left = _mm256_loadu_si256((const __m256i*)edge);
        const __m256i right = _mm256_loadu_si256((const __m256i*)(edge + 16u));
        const __m256i packed = _mm256_unpacklo_epi64(left, right);
        if (!_mm256_testz_si256(packed, high_bits)) { break; }
        const __m256i a = _mm256_shuffle_epi8(packed, p1_index);
        const __m256i b = _mm256_shuffle_epi8(packed, p0_index);
        const __m256i c = _mm256_shuffle_epi8(packed, q0_index);
        const __m256i e = _mm256_shuffle_epi8(packed, q1_index);
        const __m256i delta = _mm256_sub_epi16(c, b);
        const __m256i step = _mm256_abs_epi16(delta);
        const __m256i samples = _mm256_or_si256(_mm256_or_si256(a, b), _mm256_or_si256(c, e));
        const __m256i mask = eligible(a, b, c, e, step, samples, &gate);
        if (_mm256_movemask_epi8(mask) == 0) { continue; }
        const __m256i d = correction(a, e, delta, step, gate.clip);
        const __m256i ds = _mm256_srai_epi16(d, 1);
        /* Place (+ds, +d, -d, -ds) on the four edge samples. */
        __m256i offset = _mm256_blend_epi16(ds, d, 0x66);
        offset = _mm256_sign_epi16(offset, _mm256_setr_epi16(
            1, 1, -1, -1, 1, 1, -1, -1, 1, 1, -1, -1, 1, 1, -1, -1));
        const __m256i result = clamp_samples(_mm256_add_epi16(packed, offset), gate.maxv);
        const __m256i updated = _mm256_blendv_epi8(packed, result, mask);
        _mm256_storeu_si256((__m256i*)edge, _mm256_blend_epi32(left, updated, 0x33));
        _mm256_storeu_si256((__m256i*)(edge + 16u),
                            _mm256_blend_epi32(right, _mm256_srli_si256(updated, 8), 0x33));
    }
    return x;
}

#else

typedef int topos_deblock_avx2_tu_placeholder;

#endif
