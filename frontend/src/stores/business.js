import {loadAll} from '../api/business.js';
export const store={session:JSON.parse(localStorage.getItem('session')||'null'),data:{sources:[],facets:{departments:[],periods:[],active_store:'legacy'},periods:[],policies:[],runs:[],schedules:[],deliveries:[],audits:[],migration:null},selected:null,sourceFilter:{department:'',period:''}};
export async function reload(){store.data=await loadAll(store.sourceFilter)}
export function setSession(value){store.session=value;if(value)localStorage.setItem('session',JSON.stringify(value));else localStorage.removeItem('session')}
