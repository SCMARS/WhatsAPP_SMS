export type Lead = {
  phone: string;
  lead_id: string;
  lead_name?: string;
  campaign_external_id?: string;
  initial_message?: string;
  batch_index?: number;
};

export type BulkPayload = {
  campaign_external_id: string;
  leads: Lead[];
};

export type StopPayload = {
  phones: string[];
};
