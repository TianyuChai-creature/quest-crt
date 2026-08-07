"""Coordinate-system transformations for Quest CRT pose positions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence, TypeAlias

AxisName: TypeAlias = Literal["x", "y", "z"]
SignedAxis: TypeAlias = Literal["x", "-x", "y", "-y", "z", "-z"]
Point3: TypeAlias = tuple[float, float, float]
Quaternion: TypeAlias = tuple[float, float, float, float]
Matrix3x3: TypeAlias = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


@dataclass(frozen=True, slots=True)
class AxisTransform:
    """Signed axis permutation applied as ``point_out = matrix @ point_in``.

    Each item describes one output component. For example,
    ``("-z", "-x", "y")`` means:

    ``x_out=-z_in, y_out=-x_in, z_out=y_in``.
    """

    axes: tuple[SignedAxis, SignedAxis, SignedAxis]

    def __post_init__(self) -> None:
        normalized = tuple(str(axis).lower().strip() for axis in self.axes)
        if len(normalized) != 3:
            raise ValueError("axes must contain exactly three entries")
        if any(axis not in {"x", "-x", "y", "-y", "z", "-z"} for axis in normalized):
            raise ValueError("each axis must be one of x, -x, y, -y, z, -z")
        source_axes = [axis.removeprefix("-") for axis in normalized]
        if set(source_axes) != {"x", "y", "z"}:
            raise ValueError("axes must use each source axis exactly once")
        object.__setattr__(self, "axes", normalized)

    @property
    def matrix(self) -> Matrix3x3:
        """Return the signed permutation as a 3×3 matrix."""

        rows: list[tuple[float, float, float]] = []
        for axis in self.axes:
            row = [0.0, 0.0, 0.0]
            source = axis.removeprefix("-")
            row[_AXIS_INDEX[source]] = -1.0 if axis.startswith("-") else 1.0
            rows.append(tuple(row))
        return (rows[0], rows[1], rows[2])

    @property
    def determinant(self) -> int:
        """Return ``+1`` for handedness-preserving and ``-1`` for reflecting."""

        matrix = self.matrix
        value = (
            matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
            - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
            + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0])
        )
        return int(value)

    @property
    def changes_handedness(self) -> bool:
        """Whether this transformation reflects the input coordinate system."""

        return self.determinant < 0

    def apply(self, point: Sequence[float]) -> Point3:
        """Transform one XYZ point."""

        if len(point) != 3:
            raise ValueError("point must contain exactly three values")
        source = (float(point[0]), float(point[1]), float(point[2]))
        matrix = self.matrix
        return (
            sum(matrix[0][index] * source[index] for index in range(3)),
            sum(matrix[1][index] * source[index] for index in range(3)),
            sum(matrix[2][index] * source[index] for index in range(3)),
        )

    def apply_orientation(self, quaternion: Sequence[float]) -> Quaternion:
        """Transform an XYZW quaternion into the output coordinate basis."""

        source_rotation = quaternion_to_matrix(quaternion)
        basis = self.matrix
        output_rotation = _matrix_multiply(
            _matrix_multiply(basis, source_rotation),
            _matrix_transpose(basis),
        )
        return matrix_to_quaternion(output_rotation)


def remap_axes(axes: Sequence[str]) -> AxisTransform:
    """Create an arbitrary signed axis permutation.

    Example: ``remap_axes(("-z", "-x", "y"))`` creates WebXR→FLU.
    """

    if len(axes) != 3:
        raise ValueError("axes must contain exactly three entries")
    return AxisTransform(tuple(axes))  # type: ignore[arg-type]


def flip_axis(axis: AxisName) -> AxisTransform:
    """Create a transform that negates one axis without reordering."""

    normalized = axis.lower()
    if normalized not in _AXIS_INDEX:
        raise ValueError("axis must be x, y, or z")
    axes = ["x", "y", "z"]
    index = _AXIS_INDEX[normalized]
    axes[index] = f"-{normalized}"
    return remap_axes(axes)


# Every preset maps the Pose v4 body convention (X forward, Y up, Z right)
# into the named output convention. Presets rotate/reflect axes only; they do
# not restore the discarded local-floor translation.
COORDINATE_PRESETS: Mapping[str, AxisTransform] = MappingProxyType(
    {
        "body": remap_axes(("x", "y", "z")),
        "webxr": remap_axes(("z", "y", "-x")),
        "rfu": remap_axes(("z", "x", "y")),
        "flu": remap_axes(("x", "-z", "y")),
    }
)

# Current Pose v4 input is already spine-upper-relative X-forward/Y-up/Z-right.
DEFAULT_COORDINATE_PRESET = "body"


def transform_pose_frame(
    frame: Mapping[str, Any],
    transform: AxisTransform,
) -> dict[str, Any]:
    """Return a transformed copy of a Quest CRT pose-frame dictionary.

    Hand points, wrist orientations, elbow positions, and shoulder positions are transformed.
    ``None`` values and all protocol metadata remain unchanged.
    """

    result = deepcopy(dict(frame))
    for side in ("left", "right"):
        hand = result["hands"][side]
        hand["points"] = [
            list(transform.apply(point)) if point is not None else None for point in hand["points"]
        ]
        wrist_orientation = hand.get("wrist_orientation")
        if wrist_orientation is not None:
            hand["wrist_orientation"] = list(transform.apply_orientation(wrist_orientation))
        for joint_group in ("elbows", "shoulders"):
            joints = result.get(joint_group)
            if joints is None:
                continue
            joint = joints[side]
            if joint["position"] is not None:
                joint["position"] = list(transform.apply(joint["position"]))
    return result


def quaternion_to_matrix(quaternion: Sequence[float]) -> Matrix3x3:
    """Convert an XYZW quaternion to a 3×3 rotation matrix."""

    if len(quaternion) != 4:
        raise ValueError("quaternion must contain exactly four values")
    x, y, z, w = (float(value) for value in quaternion)
    norm = (x * x + y * y + z * z + w * w) ** 0.5
    if norm == 0:
        raise ValueError("quaternion must have non-zero norm")
    x, y, z, w = (value / norm for value in (x, y, z, w))
    return (
        (
            1 - 2 * (y * y + z * z),
            2 * (x * y - z * w),
            2 * (x * z + y * w),
        ),
        (
            2 * (x * y + z * w),
            1 - 2 * (x * x + z * z),
            2 * (y * z - x * w),
        ),
        (
            2 * (x * z - y * w),
            2 * (y * z + x * w),
            1 - 2 * (x * x + y * y),
        ),
    )


def matrix_to_quaternion(matrix: Matrix3x3) -> Quaternion:
    """Convert a 3×3 rotation matrix to a normalized XYZW quaternion."""

    trace = matrix[0][0] + matrix[1][1] + matrix[2][2]
    if trace > 0:
        scale = (trace + 1.0) ** 0.5 * 2
        x = (matrix[2][1] - matrix[1][2]) / scale
        y = (matrix[0][2] - matrix[2][0]) / scale
        z = (matrix[1][0] - matrix[0][1]) / scale
        w = 0.25 * scale
    elif matrix[0][0] > matrix[1][1] and matrix[0][0] > matrix[2][2]:
        scale = (1.0 + matrix[0][0] - matrix[1][1] - matrix[2][2]) ** 0.5 * 2
        x = 0.25 * scale
        y = (matrix[0][1] + matrix[1][0]) / scale
        z = (matrix[0][2] + matrix[2][0]) / scale
        w = (matrix[2][1] - matrix[1][2]) / scale
    elif matrix[1][1] > matrix[2][2]:
        scale = (1.0 + matrix[1][1] - matrix[0][0] - matrix[2][2]) ** 0.5 * 2
        x = (matrix[0][1] + matrix[1][0]) / scale
        y = 0.25 * scale
        z = (matrix[1][2] + matrix[2][1]) / scale
        w = (matrix[0][2] - matrix[2][0]) / scale
    else:
        scale = (1.0 + matrix[2][2] - matrix[0][0] - matrix[1][1]) ** 0.5 * 2
        x = (matrix[0][2] + matrix[2][0]) / scale
        y = (matrix[1][2] + matrix[2][1]) / scale
        z = 0.25 * scale
        w = (matrix[1][0] - matrix[0][1]) / scale

    norm = (x * x + y * y + z * z + w * w) ** 0.5
    quaternion = (x / norm, y / norm, z / norm, w / norm)
    if quaternion[3] < 0:
        return tuple(-value for value in quaternion)  # type: ignore[return-value]
    return quaternion


def wrist_local_to_world(
    point: Sequence[float],
    wrist_position: Sequence[float],
    wrist_orientation: Sequence[float],
) -> Point3:
    """Apply a wrist pose to one wrist-local point."""

    local = _point3(point)
    origin = _point3(wrist_position)
    rotation = quaternion_to_matrix(wrist_orientation)
    rotated = _matrix_vector_multiply(rotation, local)
    return tuple(rotated[index] + origin[index] for index in range(3))  # type: ignore[return-value]


def world_to_wrist_local(
    point: Sequence[float],
    wrist_position: Sequence[float],
    wrist_orientation: Sequence[float],
) -> Point3:
    """Express one world-space point in the wrist's local coordinate frame."""

    world = _point3(point)
    origin = _point3(wrist_position)
    delta = tuple(world[index] - origin[index] for index in range(3))
    inverse_rotation = _matrix_transpose(quaternion_to_matrix(wrist_orientation))
    return _matrix_vector_multiply(inverse_rotation, delta)


def to_hts_wrist_relative_frame(frame: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a world-space CRT frame to HTS-style per-wrist hand data.

    Wrist poses, elbows, and shoulders remain in the frame's world coordinate basis.
    Each hand's 21 landmarks are expressed in that hand's wrist frame.
    """

    result = deepcopy(dict(frame))
    result["representation"] = "hts-wrist-relative"
    for side in ("left", "right"):
        source_hand = result["hands"][side]
        world_points = source_hand["points"]
        wrist_position = world_points[0]
        wrist_orientation = source_hand.get("wrist_orientation")
        wrist_tracked = wrist_position is not None and wrist_orientation is not None

        if wrist_tracked:
            landmarks = [
                (
                    list(world_to_wrist_local(point, wrist_position, wrist_orientation))
                    if point is not None
                    else None
                )
                for point in world_points
            ]
            if landmarks[0] is not None:
                landmarks[0] = [0.0, 0.0, 0.0]
        else:
            landmarks = [None for _ in world_points]

        result["hands"][side] = {
            "tracked": bool(source_hand["tracked"] and wrist_tracked),
            "wrist": {
                "position": wrist_position,
                "orientation": wrist_orientation,
            },
            "landmarks": landmarks,
        }
    return result


def _point3(point: Sequence[float]) -> Point3:
    if len(point) != 3:
        raise ValueError("point must contain exactly three values")
    return (float(point[0]), float(point[1]), float(point[2]))


def _matrix_vector_multiply(matrix: Matrix3x3, vector: Sequence[float]) -> Point3:
    return tuple(
        sum(matrix[row][column] * vector[column] for column in range(3)) for row in range(3)
    )  # type: ignore[return-value]


def _matrix_transpose(matrix: Matrix3x3) -> Matrix3x3:
    return tuple(tuple(matrix[column][row] for column in range(3)) for row in range(3))  # type: ignore[return-value]


def _matrix_multiply(left: Matrix3x3, right: Matrix3x3) -> Matrix3x3:
    return tuple(
        tuple(
            sum(left[row][index] * right[index][column] for index in range(3))
            for column in range(3)
        )
        for row in range(3)
    )  # type: ignore[return-value]
