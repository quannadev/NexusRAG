import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createMcpExpressApp } from "@modelcontextprotocol/sdk/server/express.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { randomUUID } from "node:crypto";
import { z } from "zod";
import axios from "axios";

// Constants
const API_BASE_URL = process.env.API_BASE_URL || "http://localhost:8080/api/v1";

// Server Setup
const server = new McpServer({
  name: "nexusrag-mcp-server",
  version: "1.0.0",
});

// Tools Registration
server.registerTool(
  "get_workspace_list",
  {
    description: "List all knowledge bases / workspaces available in NexusRAG.",
  },
  async () => {
    try {
      const response = await axios.get(`${API_BASE_URL}/workspaces`);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(response.data, null, 2),
          },
        ],
      };
    } catch (error: any) {
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
);

server.registerTool(
  "get_document_by_id",
  {
    description: "Get details for a specific document by its ID.",
    inputSchema: {
      document_id: z.number().describe("The ID of the document to retrieve."),
    },
  },
  async ({ document_id }) => {
    try {
      const response = await axios.get(`${API_BASE_URL}/documents/${document_id}`);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(response.data, null, 2),
          },
        ],
      };
    } catch (error: any) {
      return {
        content: [
          {
            type: "text",
            text: `Failed to fetch document ${document_id}: ${error.response?.data?.detail || error.message
              }`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.registerTool(
  "query",
  {
    description: "Query indexed documents using semantic search in a specific workspace.",
    inputSchema: {
      workspace_id: z.number().describe("The ID of the workspace to query."),
      question: z.string().describe("The question to query."),
      top_k: z.number().optional().describe("Number of chunks to retrieve (default: 5)."),
      mode: z.string().optional().describe("Search mode: hybrid (default), vector_only, naive, local, global"),
    },
  },
  async ({ workspace_id, question, top_k = 5, mode = "hybrid" }) => {
    try {
      const payload = {
        question,
        top_k,
        mode,
      };
      const response = await axios.post(`${API_BASE_URL}/rag/query/${workspace_id}`, payload);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(response.data, null, 2),
          },
        ],
      };
    } catch (error: any) {
      return {
        content: [
          {
            type: "text",
            text: `Query failed for workspace ${workspace_id}: ${error.response?.data?.detail || error.message
              }`,
          },
        ],
        isError: true,
      };
    }
  }
);

// Run Server
async function main() {
  const transportType = process.env.TRANSPORT === "http" ? "http" : "stdio";

  if (transportType === "http") {
    const app = createMcpExpressApp();
    const port = process.env.PORT || 8000;

    // Store active transports
    const transports: Record<string, StreamableHTTPServerTransport> = {};

    // Single endpoint /mcp handles GET, POST, DELETE requests
    app.all("/mcp", async (req, res) => {
      try {
        const sessionId = req.headers["mcp-session-id"] as string;
        let transport: StreamableHTTPServerTransport | undefined;

        if (sessionId && transports[sessionId]) {
          transport = transports[sessionId];
        } else if (!sessionId && req.method === "POST" && isInitializeRequest(req.body)) {
          transport = new StreamableHTTPServerTransport({
            sessionIdGenerator: () => randomUUID(),
            onsessioninitialized: (newSessionId) => {
              transports[newSessionId] = transport!;
            }
          });

          transport.onclose = () => {
            const sid = transport?.sessionId;
            if (sid && transports[sid]) {
              delete transports[sid];
            }
          };

          await server.connect(transport);
        } else {
          res.status(400).json({
            jsonrpc: "2.0",
            error: {
              code: -32000,
              message: "Bad Request: No valid session ID and not an initialization request",
            },
            id: null
          });
          return;
        }

        await transport.handleRequest(req as any, res as any, req.body);
      } catch (error) {
        if (!res.headersSent) {
          res.status(500).json({
            jsonrpc: "2.0",
            error: {
              code: -32603,
              message: "Internal server error"
            },
            id: null
          });
        }
      }
    });

    app.listen(port, () => {
      console.log(`NexusRAG MCP server running on Streamable HTTP at http://localhost:${port}/mcp`);
    });

    // Cleanup handlers
    process.on("SIGINT", async () => {
      for (const sid in transports) {
        await transports[sid].close();
      }
      process.exit(0);
    });
  } else {
    const transport = new StdioServerTransport();
    await server.connect(transport);
    console.error("NexusRAG MCP server running on stdio");
  }
}

main().catch((error) => {
  console.error("Server error:", error);
  process.exit(1);
});

