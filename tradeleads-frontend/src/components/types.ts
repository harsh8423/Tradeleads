export interface TopContact {
    entity_type: string;
    value: string;
    label?: string;
}

export interface DiscoverCard {
    domain: string;
    title?: string;
    description?: string;
    category?: string;
    location?: string;
    quality_score: number;
    email_count: number;
    phone_count: number;
    linkedin_count: number;
    social_count: number;
    has_employees: boolean;
    top_contacts: TopContact[];
}

export interface DiscoverResponse {
    cards: DiscoverCard[];
    total_matched: number;
    has_more: boolean;
    batch_returned: number;
}

export interface SavedLead {
    domain: string;
    title?: string;
    category?: string;
    quality_score: number;
}
