
import numpy as np

def get_xyz_matrix(primaries, white_point):
    """
    Calculate RGB to XYZ matrix given primaries and white point.
    primaries: [[rx, ry], [gx, gy], [bx, by]]
    white_point: [wx, wy]
    """
    rx, ry = primaries[0]
    gx, gy = primaries[1]
    bx, by = primaries[2]
    wx, wy = white_point
    
    # z = 1 - x - y
    rz = 1 - rx - ry
    gz = 1 - gx - gy
    bz = 1 - bx - by
    wz = 1 - wx - wy
    
    # M * [Sr, Sg, Sb] = [Xw, Yw, Zw]
    # [Xr Xg Xb] [Sr]   [Xw]
    # [Yr Yg Yb] [Sg] = [Yw]
    # [Zr Zg Zb] [Sb]   [Zw]
    # Where Yw = 1.0 (Luminance)
    
    Xw = wx / wy
    Yw = 1.0
    Zw = wz / wy
    
    M_primaries = np.array([
        [rx, gx, bx],
        [ry, gy, by],
        [rz, gz, bz]
    ])
    
    S = np.linalg.solve(M_primaries, np.array([Xw, Yw, Zw]))
    
    M = M_primaries * S
    return M

def bradford_adaptation(source_white, target_white):
    """
    Calculate Bradford chromatic adaptation matrix.
    source_white, target_white: [x, y]
    """
    # Convert xy to XYZ
    def xy_to_XYZ(xy):
        x, y = xy
        return np.array([x/y, 1.0, (1-x-y)/y])

    Ws = xy_to_XYZ(source_white)
    Wt = xy_to_XYZ(target_white)
    
    # Bradford cone response matrix
    M_A = np.array([
        [0.8951000, 0.2664000, -0.1614000],
        [-0.7502000, 1.7135000, 0.0367000],
        [0.0389000, -0.0685000, 1.0296000]
    ])
    
    Rs = M_A @ Ws
    Rt = M_A @ Wt
    
    M_gain = np.array([
        [Rt[0]/Rs[0], 0, 0],
        [0, Rt[1]/Rs[1], 0],
        [0, 0, Rt[2]/Rs[2]]
    ])
    
    M_adapt = np.linalg.inv(M_A) @ M_gain @ M_A
    return M_adapt

def format_mat3(M):
    # GLSL mat3 is column-major construction: mat3(col0, col1, col2)
    # But usually we write it in code as:
    # mat3(
    #   m00, m01, m02,
    #   m10, m11, m12,
    #   m20, m21, m22
    # )
    # which is actually filling columns if following standard GLSL constructor:
    # mat3(float, float, ...) takes elements in column-major order.
    # So the first 3 floats are Column 0.
    
    # Let's verify what the python output is.
    # M in numpy is typically Row-Major (m[row][col]).
    # Transformation: v_out = M * v_in
    # In GLSL: v_out = M * v_in.
    # GLSL Matrix multiplication uses standard linear algebra.
    
    # However, GLSL constructor mat3(a,b,c, d,e,f, g,h,i) creates:
    # [ a d g ]
    # [ b e h ]
    # [ c f i ]
    # So if we want M, we need to pass [M00, M10, M20, M01, M11, M21, M02, M12, M22].
    
    # The existing shader code:
    # mat3 prophoto_to_srgb = mat3(
    #     2.034314, -0.228799, -0.008566,  <-- Looks like Column 0?
    #    -0.727536,  1.231719, -0.153283,
    #    -0.306778, -0.002920,  1.161849
    # );
    #
    # Let's check ProPhoto->sRGB math to confirm what these numbers are.
    
    return ""

# Definitions
D50 = [0.34567, 0.35850]
D65 = [0.3127, 0.3290]

# ProPhoto RGB (ROMM RGB) - D50
ProPhoto_Primaries = [[0.7347, 0.2653], [0.1596, 0.8404], [0.0366, 0.0001]]
M_ProPhoto_XYZ = get_xyz_matrix(ProPhoto_Primaries, D50)

# Chromatic Adaptation D50 -> D65
M_Adapt = bradford_adaptation(D50, D65)

# 1. BMD Film Gen 5
BMD_Primaries = [[0.7600, 0.2600], [0.1800, 0.7700], [0.0800, -0.0600]]
M_BMD_XYZ = get_xyz_matrix(BMD_Primaries, D65)
M_BMD_inv = np.linalg.inv(M_BMD_XYZ)
M_ProPhoto_to_BMD = M_BMD_inv @ M_Adapt @ M_ProPhoto_XYZ

# 2. Panasonic V-Gamut
VGamut_Primaries = [[0.730, 0.280], [0.165, 0.840], [0.100, -0.030]]
M_VGamut_XYZ = get_xyz_matrix(VGamut_Primaries, D65)
M_VGamut_inv = np.linalg.inv(M_VGamut_XYZ)
M_ProPhoto_to_VGamut = M_VGamut_inv @ M_Adapt @ M_ProPhoto_XYZ

# 3. Sony S-Gamut3
SGamut3_Primaries = [[0.730, 0.280], [0.140, 0.855], [0.100, -0.050]]
M_SGamut3_XYZ = get_xyz_matrix(SGamut3_Primaries, D65)
M_SGamut3_inv = np.linalg.inv(M_SGamut3_XYZ)
M_ProPhoto_to_SGamut3 = M_SGamut3_inv @ M_Adapt @ M_ProPhoto_XYZ

# 4. Check ProPhoto -> sRGB to understand layout
sRGB_Primaries = [[0.64, 0.33], [0.30, 0.60], [0.15, 0.06]]
M_sRGB_XYZ = get_xyz_matrix(sRGB_Primaries, D65)
M_sRGB_inv = np.linalg.inv(M_sRGB_XYZ)
M_ProPhoto_to_sRGB = M_sRGB_inv @ M_Adapt @ M_ProPhoto_XYZ

print("--- ProPhoto to sRGB (Reference) ---")
print(M_ProPhoto_to_sRGB)
print("\nShader sRGB Values (Column-Major?):")
print("Col0: 2.034314, -0.727536, -0.306778") 
print("Col1: -0.228799, 1.231719, -0.002920") 
print("Col2: -0.008566, -0.153283, 1.161849")

print("\n--- ProPhoto to BMD Gen 5 ---")
print(M_ProPhoto_to_BMD)

print("\n--- ProPhoto to Panasonic V-Gamut ---")
print(M_ProPhoto_to_VGamut)

print("\n--- ProPhoto to Sony S-Gamut3 ---")
print(M_ProPhoto_to_SGamut3)

