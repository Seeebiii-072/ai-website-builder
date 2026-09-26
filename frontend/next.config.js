/** @type {import('next').NextConfig} */

const nextConfig = {
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: 'http://48.217.184.55:8000/api/:path*',
      },
    ];
  },
};

module.exports = nextConfig;
