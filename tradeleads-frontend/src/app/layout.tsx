import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'TradeLeads.io API',
  description: 'B2B contact & company data for trade, logistics and commodity sectors',
}

export const viewport = {
  width: 'device-width',
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en">
      <body>
        {children}

        {/* Footer */}
        <footer style={{
          marginTop: 80,
          padding: '32px 24px',
          borderTop: '1px solid var(--border)',
          textAlign: 'center',
          color: 'var(--text-muted)',
          fontSize: 13
        }}>
          <div className="container">
            <p style={{ marginBottom: 6 }}>🌐 <strong>TradeLeads.io</strong> — Premium business data</p>
            <p>Data refreshed continuously · 2M+ companies · Trade, Logistics & Commodity sectors</p>
          </div>
        </footer>
      </body>
    </html>
  )
}
