/**
 * Server-side proxy to the backend API.
 *
 * Client components (the import wizard) need to call the API from the browser.
 * Routing those calls through here keeps the tenant API key on the server -- it
 * is attached at this hop and never shipped to the browser.
 */
import { NextRequest, NextResponse } from "next/server";

const BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";
const API_KEY = process.env.API_KEY ?? "";

/** Only these prefixes may be reached from the browser. */
const ALLOWED = [/^imports(\/|$)/, /^processes(\/|$)/, /^findings(\/|$)/, /^opportunities(\/|$)/];

function targetUrl(path: string[], search: string): string | null {
  const joined = path.join("/");
  if (!ALLOWED.some((pattern) => pattern.test(joined))) return null;
  return `${BASE_URL}/v1/${joined}${search}`;
}

async function forward(request: NextRequest, path: string[]): Promise<NextResponse> {
  const url = targetUrl(path, request.nextUrl.search);
  if (!url) {
    return NextResponse.json({ detail: "path not proxied" }, { status: 403 });
  }

  const headers = new Headers();
  if (API_KEY) headers.set("X-API-Key", API_KEY);

  const contentType = request.headers.get("content-type") ?? "";
  let body: BodyInit | undefined;
  if (request.method !== "GET") {
    if (contentType.startsWith("multipart/form-data")) {
      // Let fetch regenerate the multipart boundary from the parsed form.
      body = await request.formData();
    } else {
      headers.set("Content-Type", "application/json");
      body = await request.text();
    }
  }

  const response = await fetch(url, { method: request.method, headers, body, cache: "no-store" });
  const text = await response.text();
  return new NextResponse(text, {
    status: response.status,
    headers: { "Content-Type": response.headers.get("content-type") ?? "application/json" },
  });
}

export async function GET(request: NextRequest, { params }: { params: { path: string[] } }) {
  return forward(request, params.path);
}

export async function POST(request: NextRequest, { params }: { params: { path: string[] } }) {
  return forward(request, params.path);
}
