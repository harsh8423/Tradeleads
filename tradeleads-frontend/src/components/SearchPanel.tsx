import React from 'react';

interface SearchPanelProps {
    aiMode: boolean;
    setAiMode: (v: boolean) => void;
    keyword: string;
    setKeyword: (v: string) => void;
    category: string;
    setCategory: (v: string) => void;
    country: string;
    setCountry: (v: string) => void;
    contactType: string;
    setContactType: (v: string) => void;
    handleSearch: (e: React.FormEvent) => void;
    loading: boolean;
    hasSearched: boolean;
    onReset: () => void;
    savedCount: number;
    openDrawer: () => void;
    isInitialSearchAction: boolean;
}

export function SearchPanel(props: SearchPanelProps) {
    const {
        aiMode, setAiMode, keyword, setKeyword,
        category, setCategory, country, setCountry,
        contactType, setContactType, handleSearch,
        loading, hasSearched, onReset, savedCount, openDrawer, isInitialSearchAction
    } = props;

    return (
        <div className="search-panel">
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
                <div className="search-panel-title">✨ AI Lead Discovery</div>
            </div>

            <form onSubmit={handleSearch}>
                <div className="form-grid">
                    <div className="form-group" style={{ marginBottom: 0 }}>
                        <label>✨ Describe what you need</label>
                        <input
                            value={keyword}
                            onChange={e => setKeyword(e.target.value)}
                            placeholder="e.g. chemical exporters in India with verified email"
                        />
                    </div>
                </div>

                <div style={{ display: "flex", gap: 10, marginTop: 16, alignItems: "center", flexWrap: "wrap" }}>
                    <button className="btn btn-primary" type="submit" disabled={loading} style={{ flex: "1 1 140px" }}>
                        {loading && isInitialSearchAction ? "🔄 Loading…" : "🚀 Discover Leads"}
                    </button>

                    {hasSearched && (
                        <button
                            className="btn btn-secondary"
                            type="button"
                            onClick={onReset}
                            style={{ flex: "1 1 100px" }}
                        >
                            ✕ Reset
                        </button>
                    )}

                    <button
                        className="btn btn-secondary"
                        type="button"
                        onClick={openDrawer}
                        style={{ flex: "1 1 140px" }}
                    >
                        🗂 Saved Leads
                        {savedCount > 0 && <span className="save-badge">{savedCount}</span>}
                    </button>
                </div>
            </form>
        </div>
    );
}
