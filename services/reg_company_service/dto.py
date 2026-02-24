"""
DTO сообщений Kafka: enable_reg_company, disable_reg_company.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class EnableRegCompanyDTO(BaseModel):
    """Включение рега: topic enable_reg_company."""

    reg: str = Field(..., description="Новый/текущий рег")
    oldReg: str = Field(..., description="Старый рег (при переносе)")
    companyName: str = Field(..., description="Название компании")


class DisableRegCompanyDTO(BaseModel):
    """Выключение рега: topic disable_reg_company."""

    reg: str = Field(..., description="Рег")
    companyName: str = Field(..., description="Название компании")
