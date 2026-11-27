#ifndef TOPOS_INTERNAL_DEBLOCK_AVX2_H
#define TOPOS_INTERNAL_DEBLOCK_AVX2_H

#include <stdint.h>

#if defined(__x86_64__) || defined(_M_X64)

/* Run the vector prefix and return the first column for the scalar tail. */
uint32_t tc_deblock_vertical_row_avx2(uint16_t* row, uint32_t width,
                                     int32_t alpha, int32_t beta, int32_t clip,
                                     int32_t maxv);
uint32_t tc_deblock_horizontal_row_avx2(uint16_t* p1, uint16_t* p0,
                                       uint16_t* q0, uint16_t* q1, uint32_t width,
                                       int32_t alpha, int32_t beta, int32_t clip,
                                       int32_t maxv);

#endif
#endif
