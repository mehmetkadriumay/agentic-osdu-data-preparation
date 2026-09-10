export interface InventoryItem {
  file: {
    file_id: string;
    workspace_id?: string;
    relative_path: string;
    size_bytes: number;
    sha256?: string;
    modified_at?: string;
    discovery_version?: number;
  };
  classification: {
    category: string;
    format_id: string | null;
    confidence: number;
    evidence_ids?: string[];
    extraction_ids?: string[];
    trust_level?: string;
  } | null;
  association: {
    review_status: string;
    method: string;
    score: number;
    manifest_id?: string;
    association_id?: string;
  } | null;
  candidate: {
    candidate_id: string;
    candidate_sha256: string;
    review_status: string;
  } | null;
  manifest_sha256?: string | null;
  evidence?: Array<string | {
    evidence_id: string;
    summary: string;
    observed_value?: string | null;
  }>;
  provenance?: Array<string | {
    provenance_id: string;
    source_ref: string;
    tool_id: string;
    tool_version: string;
  }>;
}

export interface InventoryView {
  inventory_id: string;
  state_version: number;
  active_learning_model: {
    learning_model_id: string;
    version: number;
    status: string;
  } | null;
  total_count: number;
  items: InventoryItem[];
}

export interface Filters {
  search: string;
  category: string;
  format: string;
  status: string;
}

export interface ToolResponse<T> {
  status?: string;
  output?: T;
  errors?: Array<{ code: string; message: string }>;
}
