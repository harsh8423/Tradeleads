import React from 'react';
import { SavedLead } from './types';
import { qualityClass } from './utils';

interface SavedDrawerProps {
    drawerOpen: boolean;
    setDrawerOpen: (v: boolean) => void;
    savedLeads: SavedLead[];
    setSavedLeads: React.Dispatch<React.SetStateAction<SavedLead[]>>;
}

export function SavedDrawer({ drawerOpen, setDrawerOpen, savedLeads, setSavedLeads }: SavedDrawerProps) {
    const exportSaved = () => {
        const lines = ["domain,title,category,quality_score",
            ...savedLeads.map(l =>
                `${l.domain},"${(l.title || "").replace(/"/g, "'")}",${l.category || ""},${l.quality_score}`
            )
        ];
        const blob = new Blob([lines.join("\n")], { type: "text/csv" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = "saved_leads.csv"; a.click();
        URL.revokeObjectURL(url);
    };

    return (
        <>
            <div
                className={`drawer-overlay ${drawerOpen ? "open" : ""}`}
                onClick={() => setDrawerOpen(false)}
            />
            <div className={`saved-drawer ${drawerOpen ? "open" : ""}`}>
                <div className="drawer-header">
                    <div className="drawer-title">
                        🗂 Saved Leads
                        {savedLeads.length > 0 && <span className="save-badge">{savedLeads.length}</span>}
                    </div>
                    <button className="btn-icon" onClick={() => setDrawerOpen(false)}>✕</button>
                </div>

                <div className="drawer-body">
                    {savedLeads.length === 0 ? (
                        <div style={{ textAlign: "center", padding: "40px 20px", color: "var(--text-muted)" }}>
                            <div style={{ fontSize: "2.5rem", marginBottom: 10 }}>💼</div>
                            <div style={{ fontWeight: 600, marginBottom: 6 }}>No saved leads yet</div>
                            <div style={{ fontSize: "0.8rem" }}>Swipe right on a card to save it here</div>
                        </div>
                    ) : (
                        savedLeads.map((lead) => (
                            <div key={lead.domain} className="saved-item">
                                <div style={{ flex: 1, minWidth: 0 }}>
                                    <div className="saved-item-domain">
                                        <a
                                            href={`https://${lead.domain}`}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            style={{ color: "inherit", textDecoration: "none" }}
                                        >
                                            {lead.domain}
                                        </a>
                                    </div>
                                    <div className="saved-item-title">{lead.title || "—"}</div>
                                    <div style={{ display: "flex", gap: 6, marginTop: 5, alignItems: "center" }}>
                                        {lead.category && <span className="tag tag-blue">{lead.category}</span>}
                                        <div className={`quality-label ${qualityClass(lead.quality_score)}`} style={{ fontSize: "0.65rem" }}>
                                            Q: {lead.quality_score}
                                        </div>
                                    </div>
                                </div>
                                <button
                                    className="saved-item-remove"
                                    onClick={() => setSavedLeads(prev => prev.filter(l => l.domain !== lead.domain))}
                                    title="Remove"
                                >
                                    ✕
                                </button>
                            </div>
                        ))
                    )}
                </div>

                {savedLeads.length > 0 && (
                    <div className="drawer-footer">
                        <button className="btn btn-success" style={{ flex: 1 }} onClick={exportSaved}>
                            ⬇️ Export CSV
                        </button>
                        <button
                            className="btn btn-secondary"
                            onClick={() => { if (window.confirm("Clear all saved leads?")) setSavedLeads([]); }}
                        >
                            🗑 Clear
                        </button>
                    </div>
                )}
            </div>
        </>
    );
}
