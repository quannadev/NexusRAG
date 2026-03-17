"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
const index_js_1 = require("@modelcontextprotocol/sdk/server/index.js");
const stdio_js_1 = require("@modelcontextprotocol/sdk/server/stdio.js");
const types_js_1 = require("@modelcontextprotocol/sdk/types.js");
const zod_1 = require("zod");
const axios_1 = __importDefault(require("axios"));
// Constants
const API_BASE_URL = "http://localhost:8080/api/v1";
// Server Setup
const server = new index_js_1.Server({
    name: "nexusrag-mcp-server",
    version: "1.0.0",
}, {
    capabilities: {
        tools: {},
    },
});
// Tools
server.setRequestHandler(types_js_1.ListToolsRequestSchema, async () => {
    return {
        tools: [
            {
                name: "get_workspace_list",
                description: "List all knowledge bases / workspaces available in NexusRAG.",
                inputSchema: {
                    type: "object",
                    properties: {},
                },
            },
            {
                name: "get_document_by_id",
                description: "Get details for a specific document by its ID.",
                inputSchema: {
                    type: "object",
                    properties: {
                        document_id: {
                            type: "number",
                            description: "The ID of the document to retrieve.",
                        },
                    },
                    required: ["document_id"],
                },
            },
            {
                name: "query",
                description: "Query indexed documents using semantic search in a specific workspace.",
                inputSchema: {
                    type: "object",
                    properties: {
                        workspace_id: {
                            type: "number",
                            description: "The ID of the workspace to query.",
                        },
                        question: {
                            type: "string",
                            description: "The question to query.",
                        },
                        top_k: {
                            type: "number",
                            description: "Number of chunks to retrieve (default: 5).",
                        },
                        mode: {
                            type: "string",
                            description: "Search mode: hybrid (default), vector_only, naive, local, global",
                        },
                    },
                    required: ["workspace_id", "question"],
                },
            },
        ],
    };
});
// Tool Handlers
server.setRequestHandler(types_js_1.CallToolRequestSchema, async (request) => {
    switch (request.params.name) {
        case "get_workspace_list": {
            try {
                const response = await axios_1.default.get(`${API_BASE_URL}/workspaces`);
                return {
                    content: [
                        {
                            type: "text",
                            text: JSON.stringify(response.data, null, 2),
                        },
                    ],
                };
            }
            catch (error) {
                return {
                    content: [
                        {
                            type: "text",
                            text: `Failed to fetch workspaces: ${error.response?.data?.detail || error.message}`,
                        },
                    ],
                    isError: true,
                };
            }
        }
        case "get_document_by_id": {
            const { document_id } = request.params.arguments;
            if (!document_id) {
                throw new Error("Missing required parameter: document_id");
            }
            try {
                const response = await axios_1.default.get(`${API_BASE_URL}/documents/${document_id}`);
                return {
                    content: [
                        {
                            type: "text",
                            text: JSON.stringify(response.data, null, 2),
                        },
                    ],
                };
            }
            catch (error) {
                return {
                    content: [
                        {
                            type: "text",
                            text: `Failed to fetch document ${document_id}: ${error.response?.data?.detail || error.message}`,
                        },
                    ],
                    isError: true,
                };
            }
        }
        case "query": {
            const { workspace_id, question, top_k = 5, mode = "hybrid" } = request.params.arguments;
            if (!workspace_id || !question) {
                throw new Error("Missing required parameters: workspace_id, question");
            }
            try {
                const payload = {
                    question,
                    top_k,
                    mode,
                };
                const response = await axios_1.default.post(`${API_BASE_URL}/rag/query/${workspace_id}`, payload);
                return {
                    content: [
                        {
                            type: "text",
                            text: JSON.stringify(response.data, null, 2),
                        },
                    ],
                };
            }
            catch (error) {
                return {
                    content: [
                        {
                            type: "text",
                            text: `Query failed for workspace ${workspace_id}: ${error.response?.data?.detail || error.message}`,
                        },
                    ],
                    isError: true,
                };
            }
        }
        default:
            throw new Error("Unknown tool");
    }
});
// Run Server
async function main() {
    const transport = new stdio_js_1.StdioServerTransport();
    await server.connect(transport);
    console.error("NexusRAG MCP server running on stdio");
}
main().catch((error) => {
    console.error("Server error:", error);
    process.exit(1);
});
//# sourceMappingURL=index.js.map