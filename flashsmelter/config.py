"""运行期配置。

配置全部来自显式参数或 ``FLASHSMELTER_*`` 环境变量，带工艺量程校验；量程本身
就是门控的一部分（例如富氧浓度下限、余热锅炉汽包水位下限），因此校验失败要
在启动阶段直接拒绝，而不是等到运行时。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigurationError, ValidationError

ENV_PREFIX = "FLASHSMELTER_"


def _read_env(environ: Mapping[str, str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, caster in _ENV_FIELDS.items():
        raw = environ.get(ENV_PREFIX + name.upper())
        if raw is None or raw == "":
            continue
        try:
            values[name] = caster(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"环境变量 {ENV_PREFIX}{name.upper()} 取值无法解析",
                details={"value": raw, "expected": caster.__name__},
            ) from exc
    return values


_ENV_FIELDS: dict[str, Any] = {
    "host": str,
    "port": int,
    "namespace": str,
    "request_timeout_seconds": float,
    "max_body_bytes": int,
    "audit_page_limit": int,
    "burner_record_max_age_seconds": float,
    "burner_min_stable_hold_seconds": float,
    "burner_fuel_pressure_min_kpa": float,
    "burner_fuel_pressure_max_kpa": float,
    "burner_air_flow_min_nm3h": float,
    "oxygen_enrichment_min": float,
    "oxygen_enrichment_max": float,
    "oxygen_baseline_window_seconds": float,
    "oxygen_ramp_step": float,
    "oxygen_flow_min_nm3h": float,
    "oxygen_analyzer_tolerance": float,
    "feed_rate_max_tph": float,
    "heat_feed_budget_tons": float,
    "settler_min_bath_level_m": float,
    "settler_layering_dwell_seconds": float,
    "slag_min_thickness_m": float,
    "slag_tap_rate_tph": float,
    "matte_min_level_m": float,
    "matte_tap_rate_tph": float,
    "converter_batch_max_tons": float,
    "waste_drum_level_min": float,
    "waste_exhaust_temp_max_c": float,
    "waste_latch_min_hold_seconds": float,
    "furnace_purge_seconds": float,
    "furnace_min_smelt_dwell_seconds": float,
    "furnace_transition_timeout_seconds": float,
    "slagyard_cooling_bays": int,
    "slagyard_min_cool_seconds": float,
    "slagyard_cool_seconds_per_ton": float,
    "slagyard_ladle_max_tons": float,
    "slagmill_feed_rate_max_tph": float,
    "slagmill_grind_fineness_min_pct": float,
    "slagmill_grind_fineness_max_pct": float,
    "slagmill_base_recovery": float,
    "slagmill_recovery_min": float,
    "slagmill_recovery_max": float,
    "slagmill_ref_grade_pct": float,
    "slagmill_recovery_grade_slope": float,
    "slagmill_ref_fineness_pct": float,
    "slagmill_recovery_size_slope": float,
    "slagmill_conc_grade_pct": float,
    "slagmill_reagent_max_kgpt": float,
}


@dataclass(frozen=True, slots=True)
class Settings:
    """平台配置。字段默认值即一条正常运行的产线。"""

    root: Path = field(default_factory=lambda: Path("var"))
    host: str = "127.0.0.1"
    port: int = 8080
    namespace: str = "smelter/line1"
    request_timeout_seconds: float = 5.0
    max_body_bytes: int = 65536
    audit_page_limit: int = 200

    # 燃烧器状态必须已在窗口内落盘，精矿喷吹才允许 armed。
    burner_record_max_age_seconds: float = 120.0
    burner_min_stable_hold_seconds: float = 20.0
    burner_fuel_pressure_min_kpa: float = 120.0
    burner_fuel_pressure_max_kpa: float = 320.0
    burner_air_flow_min_nm3h: float = 3500.0

    # 富氧系统量程与分析仪基线窗口。
    oxygen_enrichment_min: float = 0.45
    oxygen_enrichment_max: float = 0.85
    oxygen_baseline_window_seconds: float = 300.0
    oxygen_ramp_step: float = 0.02
    oxygen_flow_min_nm3h: float = 8000.0
    oxygen_analyzer_tolerance: float = 0.01

    # 精矿喷吹与热料预算。
    feed_rate_max_tph: float = 180.0
    heat_feed_budget_tons: float = 1200.0

    # 沉淀池分层与放渣放铜阈值。
    settler_min_bath_level_m: float = 0.35
    settler_layering_dwell_seconds: float = 60.0
    slag_min_thickness_m: float = 0.08
    slag_tap_rate_tph: float = 40.0
    matte_min_level_m: float = 0.20
    matte_tap_rate_tph: float = 55.0

    # 转炉单批上限。
    converter_batch_max_tons: float = 90.0

    # 余热锅炉联锁。
    waste_drum_level_min: float = 0.40
    waste_exhaust_temp_max_c: float = 380.0
    waste_latch_min_hold_seconds: float = 30.0

    # 闪速炉编排。
    furnace_purge_seconds: float = 15.0
    furnace_min_smelt_dwell_seconds: float = 45.0
    furnace_transition_timeout_seconds: float = 600.0

    # 渣场缓冷与倒渣。
    slagyard_cooling_bays: int = 6
    slagyard_min_cool_seconds: float = 28800.0
    slagyard_cool_seconds_per_ton: float = 300.0
    slagyard_ladle_max_tons: float = 30.0

    # 磨选给矿、加药与回收模型。
    slagmill_feed_rate_max_tph: float = 60.0
    slagmill_grind_fineness_min_pct: float = 55.0
    slagmill_grind_fineness_max_pct: float = 95.0
    slagmill_base_recovery: float = 0.85
    slagmill_recovery_min: float = 0.50
    slagmill_recovery_max: float = 0.97
    slagmill_ref_grade_pct: float = 1.0
    slagmill_recovery_grade_slope: float = 0.02
    slagmill_ref_fineness_pct: float = 75.0
    slagmill_recovery_size_slope: float = 0.004
    slagmill_conc_grade_pct: float = 25.0
    slagmill_reagent_max_kgpt: float = 1.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> "Settings":
        env = os.environ if environ is None else environ
        values = _read_env(env)
        root = env.get(ENV_PREFIX + "ROOT")
        if root:
            values["root"] = Path(root)
        values.update(overrides)
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValidationError("监听端口超出范围", details={"port": self.port})
        if self.request_timeout_seconds <= 0:
            raise ValidationError(
                "请求超时必须为正数", details={"request_timeout_seconds": self.request_timeout_seconds}
            )
        if self.max_body_bytes < 1024:
            raise ValidationError("请求体上限过小", details={"max_body_bytes": self.max_body_bytes})
        if self.audit_page_limit < 1:
            raise ValidationError("审计分页上限必须为正", details={"audit_page_limit": self.audit_page_limit})
        if not 0 < self.oxygen_enrichment_min < self.oxygen_enrichment_max < 1:
            raise ValidationError(
                "富氧浓度量程不合法",
                details={
                    "min": self.oxygen_enrichment_min,
                    "max": self.oxygen_enrichment_max,
                },
            )
        if self.oxygen_ramp_step <= 0:
            raise ValidationError("富氧爬坡步长必须为正", details={"step": self.oxygen_ramp_step})
        if self.oxygen_baseline_window_seconds <= 0:
            raise ValidationError(
                "分析仪基线窗口必须为正", details={"window": self.oxygen_baseline_window_seconds}
            )
        if self.burner_record_max_age_seconds <= 0:
            raise ValidationError(
                "燃烧器落盘时效窗口必须为正",
                details={"window": self.burner_record_max_age_seconds},
            )
        if self.burner_min_stable_hold_seconds < 0:
            raise ValidationError(
                "燃烧器稳定保持时长不能为负", details={"hold": self.burner_min_stable_hold_seconds}
            )
        if not 0 < self.burner_fuel_pressure_min_kpa < self.burner_fuel_pressure_max_kpa:
            raise ValidationError(
                "燃烧器燃料压力量程不合法",
                details={
                    "min": self.burner_fuel_pressure_min_kpa,
                    "max": self.burner_fuel_pressure_max_kpa,
                },
            )
        if self.burner_air_flow_min_nm3h <= 0:
            raise ValidationError(
                "燃烧器助燃风下限必须为正", details={"min": self.burner_air_flow_min_nm3h}
            )
        if self.oxygen_flow_min_nm3h <= 0:
            raise ValidationError("富氧流量下限必须为正", details={"min": self.oxygen_flow_min_nm3h})
        if self.oxygen_analyzer_tolerance <= 0:
            raise ValidationError(
                "分析仪偏差容限必须为正", details={"tolerance": self.oxygen_analyzer_tolerance}
            )
        if self.feed_rate_max_tph <= 0:
            raise ValidationError("喷吹上限必须为正", details={"rate": self.feed_rate_max_tph})
        if self.heat_feed_budget_tons < self.feed_rate_max_tph:
            raise ValidationError(
                "单炉热料预算小于单次喷吹上限",
                details={
                    "budget": self.heat_feed_budget_tons,
                    "rate": self.feed_rate_max_tph,
                },
            )
        if self.settler_layering_dwell_seconds <= 0:
            raise ValidationError(
                "分层静置时长必须为正", details={"dwell": self.settler_layering_dwell_seconds}
            )
        if not 0 < self.slag_min_thickness_m < self.settler_min_bath_level_m:
            raise ValidationError(
                "渣层下限必须小于沉淀池最低液位",
                details={
                    "slag": self.slag_min_thickness_m,
                    "bath": self.settler_min_bath_level_m,
                },
            )
        if self.matte_min_level_m >= self.settler_min_bath_level_m:
            raise ValidationError(
                "冰铜液位下限必须小于沉淀池最低液位",
                details={"matte": self.matte_min_level_m, "bath": self.settler_min_bath_level_m},
            )
        if self.converter_batch_max_tons <= 0:
            raise ValidationError(
                "转炉单批上限必须为正", details={"max": self.converter_batch_max_tons}
            )
        if not 0 < self.waste_drum_level_min < 1:
            raise ValidationError(
                "汽包水位下限必须是 (0,1) 区间比例", details={"min": self.waste_drum_level_min}
            )
        if self.waste_exhaust_temp_max_c <= 0:
            raise ValidationError(
                "烟气温度上限必须为正", details={"max": self.waste_exhaust_temp_max_c}
            )
        if self.waste_latch_min_hold_seconds <= 0:
            raise ValidationError(
                "闩锁最短保持时长必须为正", details={"hold": self.waste_latch_min_hold_seconds}
            )
        if self.furnace_purge_seconds <= 0:
            raise ValidationError("吹扫时长必须为正", details={"purge": self.furnace_purge_seconds})
        if self.furnace_min_smelt_dwell_seconds < 0:
            raise ValidationError(
                "熔炼静置时长不能为负", details={"dwell": self.furnace_min_smelt_dwell_seconds}
            )
        if self.furnace_transition_timeout_seconds <= self.furnace_purge_seconds:
            raise ValidationError(
                "编排超时不得小于吹扫时长",
                details={
                    "timeout": self.furnace_transition_timeout_seconds,
                    "purge": self.furnace_purge_seconds,
                },
            )
        if self.slagyard_cooling_bays < 1:
            raise ValidationError("缓冷位数量必须为正", details={"bays": self.slagyard_cooling_bays})
        if self.slagyard_min_cool_seconds <= 0:
            raise ValidationError(
                "最短缓冷时长必须为正", details={"min_cool": self.slagyard_min_cool_seconds}
            )
        if self.slagyard_cool_seconds_per_ton < 0:
            raise ValidationError(
                "单位吨位缓冷时长不能为负", details={"per_ton": self.slagyard_cool_seconds_per_ton}
            )
        if self.slagyard_ladle_max_tons <= 0:
            raise ValidationError("渣包容量上限必须为正", details={"max": self.slagyard_ladle_max_tons})
        if self.slagmill_feed_rate_max_tph <= 0:
            raise ValidationError("磨机给矿上限必须为正", details={"max": self.slagmill_feed_rate_max_tph})
        if not 0 < self.slagmill_grind_fineness_min_pct < self.slagmill_grind_fineness_max_pct <= 100:
            raise ValidationError(
                "磨矿细度量程不合法",
                details={
                    "min": self.slagmill_grind_fineness_min_pct,
                    "max": self.slagmill_grind_fineness_max_pct,
                },
            )
        if not 0 < self.slagmill_recovery_min < self.slagmill_recovery_max <= 1:
            raise ValidationError(
                "回收率量程不合法",
                details={"min": self.slagmill_recovery_min, "max": self.slagmill_recovery_max},
            )
        if not self.slagmill_recovery_min <= self.slagmill_base_recovery <= self.slagmill_recovery_max:
            raise ValidationError(
                "基准回收率必须落在回收率量程内",
                details={
                    "base": self.slagmill_base_recovery,
                    "min": self.slagmill_recovery_min,
                    "max": self.slagmill_recovery_max,
                },
            )
        if self.slagmill_ref_grade_pct <= 0:
            raise ValidationError("参考品位必须为正", details={"ref_grade": self.slagmill_ref_grade_pct})
        if self.slagmill_recovery_grade_slope < 0:
            raise ValidationError(
                "品位回收系数不能为负", details={"slope": self.slagmill_recovery_grade_slope}
            )
        if not 0 < self.slagmill_ref_fineness_pct <= 100:
            raise ValidationError(
                "参考细度必须在 (0,100] 区间", details={"ref_fineness": self.slagmill_ref_fineness_pct}
            )
        if self.slagmill_recovery_size_slope < 0:
            raise ValidationError(
                "细度回收系数不能为负", details={"slope": self.slagmill_recovery_size_slope}
            )
        if not 0 < self.slagmill_conc_grade_pct < 100:
            raise ValidationError(
                "铜精矿品位必须在 (0,100) 区间", details={"conc_grade": self.slagmill_conc_grade_pct}
            )
        if self.slagmill_reagent_max_kgpt <= 0:
            raise ValidationError("加药量上限必须为正", details={"max": self.slagmill_reagent_max_kgpt})

    def with_root(self, root: Path | str) -> "Settings":
        updated = replace(self, root=Path(root))
        updated.validate()
        return updated

    def ensure_directories(self) -> Path:
        root = Path(self.root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "journal").mkdir(parents=True, exist_ok=True)
        return root

    def as_dict(self) -> dict[str, Any]:
        payload = {
            name: getattr(self, name)
            for name in self.__slots__
        }
        payload["root"] = str(self.root)
        return payload


__all__ = ["Settings", "ENV_PREFIX"]
