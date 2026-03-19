-- Cleaned up schema.sql

CREATE TYPE public.documentstatus AS ENUM (
    'PENDING',
    'PARSING',
    'PROCESSING',
    'INDEXING',
    'INDEXED',
    'FAILED'
);

CREATE TABLE public.alembic_version (
    version_num character varying(32) PRIMARY KEY
);

CREATE TABLE public.knowledge_bases (
    id SERIAL PRIMARY KEY,
    name character varying(255) NOT NULL,
    description text,
    system_prompt text,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    kg_language character varying(50),
    kg_entity_types json
);

CREATE TABLE public.chat_messages (
    id SERIAL PRIMARY KEY,
    workspace_id integer NOT NULL REFERENCES public.knowledge_bases(id) ON DELETE CASCADE,
    message_id character varying(50) NOT NULL,
    role character varying(20) NOT NULL,
    content text NOT NULL,
    sources json,
    related_entities json,
    image_refs json,
    thinking text,
    ratings json,
    agent_steps json,
    created_at timestamp without time zone NOT NULL
);

CREATE INDEX ix_chat_messages_id ON public.chat_messages USING btree (id);
CREATE INDEX ix_chat_messages_message_id ON public.chat_messages USING btree (message_id);
CREATE INDEX ix_chat_messages_workspace_id ON public.chat_messages USING btree (workspace_id);

CREATE TABLE public.documents (
    id SERIAL PRIMARY KEY,
    workspace_id integer NOT NULL REFERENCES public.knowledge_bases(id) ON DELETE CASCADE,
    filename character varying(255) NOT NULL,
    original_filename character varying(255) NOT NULL,
    file_type character varying(50) NOT NULL,
    file_size integer NOT NULL,
    status public.documentstatus NOT NULL,
    chunk_count integer NOT NULL,
    error_message character varying(500),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    page_count integer NOT NULL,
    image_count integer NOT NULL,
    table_count integer NOT NULL,
    parser_version character varying(50),
    processing_time_ms integer NOT NULL,
    custom_metadata json,
    -- S3 object storage (content-addressable via SHA-256 hash)
    file_sha256 character varying(64),
    s3_bucket character varying(255),
    s3_raw_key character varying(1000),
    s3_markdown_key character varying(1000)
);

CREATE INDEX idx_documents_file_sha256 ON public.documents USING btree (file_sha256);

CREATE TABLE public.document_images (
    id SERIAL PRIMARY KEY,
    document_id integer NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
    image_id character varying(100) NOT NULL UNIQUE,
    page_no integer NOT NULL,
    caption text NOT NULL,
    width integer NOT NULL,
    height integer NOT NULL,
    mime_type character varying(50) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    -- S3 object storage
    s3_key character varying(1000),
    s3_bucket character varying(255)
);

CREATE TABLE public.document_tables (
    id SERIAL PRIMARY KEY,
    document_id integer NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
    table_id character varying(100) NOT NULL UNIQUE,
    page_no integer NOT NULL,
    content_markdown text NOT NULL,
    caption text NOT NULL,
    num_rows integer NOT NULL,
    num_cols integer NOT NULL,
    created_at timestamp without time zone NOT NULL
);
