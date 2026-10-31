import numpy as np

# Standard White Points (xy)
WP_D50 = np.array([0.34567, 0.35850])
WP_D65 = np.array([0.3127, 0.3290])
WP_D60 = np.array([0.32168, 0.33767]) # ACES D60

def xy_to_XYZ(xy, Y=1.0):
    x, y = xy
    if y == 0: return np.array([0,0,0])
    X = (x * Y) / y
    Z = ((1 - x - y) * Y) / y
    return np.array([X, Y, Z])

def get_rgb_to_xyz_matrix(primaries_xy, wp_xy):
    # primaries_xy: list of [rx, ry], [gx, gy], [bx, by]
    # wp_xy: [wx, wy]
    rx, ry = primaries_xy[0]
    gx, gy = primaries_xy[1]
    bx, by = primaries_xy[2]
    wx, wy = wp_xy
    
    Xr, Yr, Zr = xy_to_XYZ([rx, ry])
    Xg, Yg, Zg = xy_to_XYZ([gx, gy])
    Xb, Yb, Zb = xy_to_XYZ([bx, by])
    Xw, Yw, Zw = xy_to_XYZ([wx, wy])
    
    # [Xr Xg Xb] [Sr]   [Xw]
    # [Yr Yg Yb] [Sg] = [Yw]
    # [Zr Zg Zb] [Sb]   [Zw]
    
    M = np.array([
        [Xr, Xg, Xb],
        [Yr, Yg, Yb],
        [Zr, Zg, Zb]
    ])
    
    S = np.linalg.solve(M, np.array([Xw, Yw, Zw]))
    
    M_scaled = M * S # Broadcasting: column i multiplied by S[i]
    return M_scaled

def get_bradford_matrix(source_wp, dest_wp):
    # Bradford
    # Using D50 -> D65 adaptation usually
    # M_A = M_cone^-1 * diag(rho_d/rho_s, ...) * M_cone
    
    # Source XYZ
    XYZ_s = xy_to_XYZ(source_wp)
    XYZ_d = xy_to_XYZ(dest_wp)
    
    M_cone = np.array([
        [0.8951, 0.2664, -0.1614],
        [-0.7502, 1.7135, 0.0367],
        [0.0389, -0.0685, 1.0296]
    ])
    
    LMS_s = M_cone @ XYZ_s
    LMS_d = M_cone @ XYZ_d
    
    r = LMS_d[0] / LMS_s[0]
    g = LMS_d[1] / LMS_s[1]
    b = LMS_d[2] / LMS_s[2]
    
    M_diag = np.diag([r, g, b])
    
    M_adapt = np.linalg.inv(M_cone) @ M_diag @ M_cone
    return M_adapt

# Primaries (xy)
# ProPhoto RGB (ROMM RGB)
# R: 0.7347, 0.2653
# G: 0.1596, 0.8404
# B: 0.0366, 0.0001
PROPHOTO_PRIMARIES = [[0.7347, 0.2653], [0.1596, 0.8404], [0.0366, 0.0001]]
PROPHOTO_WP = WP_D50

# sRGB / Rec.709
SRGB_PRIMARIES = [[0.6400, 0.3300], [0.3000, 0.6000], [0.1500, 0.0600]]
SRGB_WP = WP_D65

# Rec.2020
REC2020_PRIMARIES = [[0.708, 0.292], [0.170, 0.797], [0.131, 0.046]]
REC2020_WP = WP_D65

# Adobe RGB
ADOBE_PRIMARIES = [[0.6400, 0.3300], [0.2100, 0.7100], [0.1500, 0.0600]]
ADOBE_WP = WP_D65

# DCI-P3 (D65) - Display P3 uses D65, DCI-P3 uses DCI White
DISPLAY_P3_PRIMARIES = [[0.680, 0.320], [0.265, 0.690], [0.150, 0.060]]
DISPLAY_P3_WP = WP_D65

# ACEScg
ACESCG_PRIMARIES = [[0.713, 0.293], [0.165, 0.830], [0.128, 0.044]]
ACESCG_WP = WP_D60

# Calculate Matrices
def calc_conversion(source_prim, source_wp, dest_prim, dest_wp, adapt_method='Bradford'):
    M_source = get_rgb_to_xyz_matrix(source_prim, source_wp)
    M_dest = get_rgb_to_xyz_matrix(dest_prim, dest_wp)
    
    if np.allclose(source_wp, dest_wp):
        M_adapt = np.eye(3)
    else:
        M_adapt = get_bradford_matrix(source_wp, dest_wp)
        
    # RGB_in -> XYZ_source -> XYZ_dest -> RGB_out
    # XYZ_dest = M_adapt * XYZ_source
    # XYZ_source = M_source * RGB_in
    # RGB_out = inv(M_dest) * XYZ_dest
    
    M_final = np.linalg.inv(M_dest) @ M_adapt @ M_source
    return M_final

print("--- ProPhoto D50 to sRGB D65 ---")
M = calc_conversion(PROPHOTO_PRIMARIES, PROPHOTO_WP, SRGB_PRIMARIES, SRGB_WP)
print(M)

print("\n--- ProPhoto D50 to Rec.2020 D65 ---")
M = calc_conversion(PROPHOTO_PRIMARIES, PROPHOTO_WP, REC2020_PRIMARIES, REC2020_WP)
print(M)

print("\n--- ProPhoto D50 to Adobe RGB D65 ---")
M = calc_conversion(PROPHOTO_PRIMARIES, PROPHOTO_WP, ADOBE_PRIMARIES, ADOBE_WP)
print(M)

print("\n--- ProPhoto D50 to Display P3 D65 ---")
M = calc_conversion(PROPHOTO_PRIMARIES, PROPHOTO_WP, DISPLAY_P3_PRIMARIES, DISPLAY_P3_WP)
print(M)

print("\n--- ProPhoto D50 to ACEScg D60 ---")
M = calc_conversion(PROPHOTO_PRIMARIES, PROPHOTO_WP, ACESCG_PRIMARIES, ACESCG_WP)
print(M)

print("\n--- Rec.709 D65 to Rec.2020 D65 ---")
M = calc_conversion(SRGB_PRIMARIES, SRGB_WP, REC2020_PRIMARIES, REC2020_WP)
print(M)

print("\n--- ProPhoto D50 to Rec.2020 D50 ---")
M = calc_conversion(PROPHOTO_PRIMARIES, PROPHOTO_WP, REC2020_PRIMARIES, WP_D50)
print(M)

print("\n--- ProPhoto D50 to Rec.2020 D65 (Bradford) ---")
M = calc_conversion(PROPHOTO_PRIMARIES, PROPHOTO_WP, REC2020_PRIMARIES, REC2020_WP, 'Bradford')
print(M)
