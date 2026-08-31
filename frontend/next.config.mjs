/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The backend base URL is read at request time so the same build works in
  // local development and in Docker Compose.
  env: {
    API_BASE_URL: process.env.API_BASE_URL ?? "http://localhost:8000",
  },
};

export default nextConfig;
