import {request} from './client.js';

const query = (params) => {
 const search = new URLSearchParams();
 for (const [key, value] of Object.entries(params || {})) if (value) search.set(key, value);
 const text = search.toString();
 return text ? '?' + text : '';
};

export const loadAll = async (sourceFilter) => {
 const endpoints = {
  sources: '/sources' + query(sourceFilter),
  facets: '/source-facets',
  periods: '/periods',
  policies: '/policies',
  runs: '/runs',
  schedules: '/schedules',
  deliveries: '/deliveries',
  audits: '/audits',
  migration: '/migration',
 };
 return Object.fromEntries(await Promise.all(Object.entries(endpoints).map(async ([key, path]) => [key, await request(path)])));
};
export const saveSource=(data,id)=>request('/sources'+(id?'/'+id:''),id?'PUT':'POST',{data});
export const savePolicy=data=>request('/policies','POST',{data});
export const submitRun=data=>request('/runs','POST',data);
export const approveRun=(id,note)=>request('/runs/'+id+'/approve','POST',{note});
export const migrationAction=(action)=>request('/migration/'+action,'POST',{});
