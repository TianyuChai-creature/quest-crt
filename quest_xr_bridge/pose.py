"""Validated Quest pose v5 and HTTP request models."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
PosePoint = tuple[FiniteFloat, FiniteFloat, FiniteFloat]
PoseQuaternion = tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]
JointRadius = Annotated[FiniteFloat, Field(ge=0)]


class PoseHand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracked: bool = Field(strict=True)
    points: list[PosePoint | None] = Field(min_length=21, max_length=21)
    wrist_orientation: PoseQuaternion | None
    radii: list[JointRadius | None] = Field(
        default_factory=lambda: [None] * 21, min_length=21, max_length=21
    )

    @model_validator(mode="after")
    def validate_tracking(self) -> PoseHand:
        if self.tracked and any(point is None for point in self.points):
            raise ValueError("tracked hand must contain all 21 points")
        if self.tracked and self.wrist_orientation is None:
            raise ValueError("tracked hand must contain a wrist orientation")
        if (self.points[0] is None) != (self.wrist_orientation is None):
            raise ValueError("wrist point and wrist orientation availability must match")
        if self.wrist_orientation is not None and not any(self.wrist_orientation):
            raise ValueError("wrist orientation must have non-zero norm")
        if any(
            radius is not None and point is None
            for point, radius in zip(self.points, self.radii, strict=True)
        ):
            raise ValueError("joint radius requires a joint position")
        return self


class PoseJoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracked: bool = Field(strict=True)
    position: PosePoint | None

    @model_validator(mode="after")
    def validate_tracking(self) -> PoseJoint:
        if self.tracked != (self.position is not None):
            raise ValueError("tracked must match position availability")
        return self


class PoseHands(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: PoseHand
    right: PoseHand


class PoseJoints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: PoseJoint
    right: PoseJoint


class PoseHead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracked: bool = Field(strict=True)
    yaw_deg: Annotated[FiniteFloat, Field(ge=-180, le=180)] | None
    pitch_deg: Annotated[FiniteFloat, Field(ge=-90, le=90)] | None

    @model_validator(mode="after")
    def validate_tracking(self) -> PoseHead:
        if self.tracked != (self.yaw_deg is not None) or self.tracked != (
            self.pitch_deg is not None
        ):
            raise ValueError("tracked head must contain both angles")
        return self


class PoseFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pose"]
    version: Literal[5]
    session_id: UUID
    seq: int = Field(strict=True, ge=1, le=0xFFFFFFFF)
    timestamp_ms: Annotated[FiniteFloat, Field(ge=0)]
    capture_epoch_ms: Annotated[FiniteFloat, Field(ge=0)]
    reference_space: Literal["spine-upper-scapula"]
    units: Literal["meters"]
    hands: PoseHands
    elbows: PoseJoints
    shoulders: PoseJoints
    head: PoseHead


class WebRTCOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sdp: str = Field(min_length=1)
    type: Literal["offer"]


class VideoPeerClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    peer_id: UUID

    @field_validator("peer_id", mode="before")
    @classmethod
    def bounded_peer_id(cls, value):
        if not isinstance(value, UUID) and (not isinstance(value, str) or len(value) > 36):
            raise ValueError("peer_id must be a UUID of at most 36 characters")
        return value


class WebRTCVideoOffer(WebRTCOffer):
    sdp: str = Field(min_length=1, max_length=1_000_000)
    peer_id: UUID

    @field_validator("peer_id", mode="before")
    @classmethod
    def bounded_peer_id(cls, value):
        return VideoPeerClose.bounded_peer_id(value)


class CoordinateTransformRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str | None = None
    axes: list[str] | None = Field(default=None, min_length=3, max_length=3)
    name: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def exactly_one_transform_source(self) -> CoordinateTransformRequest:
        if (self.preset is None) == (self.axes is None):
            raise ValueError("provide exactly one of preset or axes")
        return self
