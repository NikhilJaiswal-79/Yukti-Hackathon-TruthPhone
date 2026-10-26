import operator
from typing import Annotated, List, Dict, Any, Optional, TypedDict
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage

class PhoneCandidate(BaseModel):
    name: str = Field(description="Name of the phone")
    price: str = Field(description="Price of the phone")
    specs: str = Field(description="Key specifications")
    url: str = Field(description="URL to the listing")
    details: str = Field(default="", description="Full details scraped from the product page")

class VerificationClaim(BaseModel):
    claim: str = Field(description="The specific claim to verify (e.g. '8GB RAM')")
    context: str = Field(description="Context of the claim in the listing")

class VerificationResult(BaseModel):
    claim: str = Field(description="The claim that was checked")
    is_verified: bool = Field(description="True if verified, False if misleading/false")
    explanation: str = Field(description="Explanation of the finding")

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    requirements: Dict[str, Any]
    ready_to_search: bool
    search_results: List[PhoneCandidate]
    comparison_summary: str
    selected_phone_index: Optional[int]
    claims_to_check: List[VerificationClaim]
    verification_results: List[VerificationResult]
    final_report: str
    current_phase: str # 'clarification', 'search', 'comparison', 'selection', 'verification', 'done'
