from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    opensearch_url: str = "http://opensearch:9200"
    index_name: str = "nrc-all-v3"

    e5_model_id: str = "intfloat/multilingual-e5-large"
    fermi_model_id: str = "atomic-canyon/fermi-1024"

    device: str = "cuda"
    e5_max_seq_len: int = 512
    fermi_max_seq_len: int = 1024

    top_k_default: int = 10
    k_dense_default: int = 50
    sparse_top_n: int = 200
    sparse_weight_threshold: float = 0.0

    dense_field: str = "dense_e5"
    sparse_field: str = "sparse_fermi"
    text_field: str = "text"

    search_pipeline: str = "nrc-hybrid-search"


settings = Settings()
