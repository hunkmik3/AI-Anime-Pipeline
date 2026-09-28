import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
const storage=new Map();
Object.defineProperty(globalThis,'localStorage',{value:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},configurable:true});
const bundled=await build({entryPoints:[fileURLToPath(new URL('../src/store/automation.ts',import.meta.url))],bundle:true,platform:'node',format:'cjs',write:false,logLevel:'silent'});
const mod={exports:{}};new Function('require','module','exports',bundled.outputFiles[0].text)(createRequire(import.meta.url),mod,mod.exports);
const store=mod.exports.useAutomation;
const tick=()=>new Promise(r=>setTimeout(r,0));
let calls=[],handler=async()=>[];
globalThis.fetch=async(path,init={})=>{const body=init.body?JSON.parse(init.body):undefined;calls.push({path,body});const out=await handler(path,body);return {ok:!out?.failure,status:out?.failure??200,json:async()=>out?.failure?{detail:out.message}:out};};
const video={id:'vid:c1',type:'autoVideo',position:{x:0,y:0},data:{kind:'video',sequenceKey:'c1',label:'Clip',title:'',prompt:'Locked draft',refs:[],status:'running',runtimeJobId:'job-1'}};
test('server jobs restore status without destroying prompt edits',()=>{
 const out=mod.exports.applyRuntimeJobs([video],[{id:'job-1',kind:'clip',node_id:'vid:c1',slot:'',status:'succeeded',provider_job_id:'upstream',error:'',result:{url:'https://test/video',persisted:true}}]);
 assert.equal(out[0].data.status,'done');assert.equal(out[0].data.prompt,'Locked draft');assert.equal(out[0].data.clipUrl,'https://test/video');assert.equal(video.data.status,'running');
});
test('opening a board keeps tracked running jobs and polls server',async()=>{
 handler=async(path)=>path.endsWith('/jobs')?[{id:'job-1',kind:'clip',node_id:'vid:c1',slot:'',status:'running',error:'',result:{}}]:path.endsWith('/projects/p1')?{id:'p1',revision:7,board:{nodes:[video],edges:[]}}:[];
 await store.getState().openProject('p1');await tick();
 assert.equal(store.getState().nodes.find(n=>n.id==='vid:c1').data.status,'running');assert.equal(store.getState().projectRevision,7);
 store.setState({currentProjectId:null});
});
test('saves serialize revisions and conflict preserves local edits',async()=>{
 calls=[];let revision=0;
 handler=async(path,body)=>{if(path.endsWith('/projects/p2')){assert.equal(body.expected_revision,revision);return {id:'p2',revision:++revision};}return [];};
 store.setState({currentProjectId:'p2',projectRevision:0,nodes:[video],script:'draft'});
 await Promise.all([store.getState().saveNow(),store.getState().saveNow()]);assert.equal(store.getState().projectRevision,2);
 handler=async(path)=>path.endsWith('/projects/p2')?{failure:409,message:'Conflict: reload board'}:[];
 store.setState({script:'local work must survive'});await store.getState().saveNow();
 assert.equal(store.getState().saveState,'error');assert.equal(store.getState().script,'local work must survive');assert.equal(store.getState().projectRevision,2);
 store.setState({currentProjectId:null});
});
test('saved board sends paid image work to durable endpoint',async()=>{
 const plate={prompt:'A studio sheet',status:'idle'};
 store.setState({currentProjectId:'p3',projectRevision:0,nodes:[{id:'char:hero',type:'autoCharacter',position:{x:0,y:0},data:{kind:'character',character:{key:'hero',name:'Hero',states:[]},identity:plate,states:{}}}],edges:[]});
 calls=[];let rev=0;
 handler=async(path,body)=>{
  if(path.endsWith('/projects/p3'))return {id:'p3',revision:++rev};
  if(path.endsWith('/p3/jobs')){assert.equal(body.kind,'plate');assert.equal(body.node_id,'char:hero');assert.equal(body.slot,'identity');return {id:'j3',kind:'plate',node_id:'char:hero',slot:'identity',status:'succeeded',error:'',result:{images:[{url:'https://test/image',reference_url:'https://test/image',media_id:'m3'}]}};}
  return [];
 };
 await store.getState().generate('char:hero','identity');
 assert.equal(calls.filter(c=>c.path==='/api/automation/plate').length,0);
 assert.equal(calls.filter(c=>c.path.endsWith('/p3/jobs')).length,1);
 assert.equal(store.getState().nodes[0].data.identity.referenceUrl,'https://test/image');
 store.setState({currentProjectId:null});
});
test('writer result cannot overwrite changed shots and latest job wins',()=>{
 const seq={id:'seq:c1',data:{kind:'sequence',sequence:{key:'c1'},shots:[{id:1,duration_s:3}]}};
 const job={id:'writer-1',kind:'write',node_id:'vid:c1',slot:'prompt',status:'succeeded',result:{base_prompt:'Locked draft',prompt:'Generated',source_shots:[{id:1,duration_s:2}]}};
 assert.equal(mod.exports.applyRuntimeJobs([video,seq],[job])[0].data.prompt,'Locked draft');
 job.result.source_shots=seq.data.shots;
 assert.equal(mod.exports.applyRuntimeJobs([video,seq],[job])[0].data.prompt,'Generated');
 const pending={...job,id:'writer-2',status:'queued',result:{}};
 assert.equal(mod.exports.applyRuntimeJobs([video,seq],[job,pending])[0].data.prompt,'Locked draft');
});
test('per-shot frame completion restores image and package version',()=>{
 const out=mod.exports.applyRuntimeJobs([video],[{id:'frame1',kind:'plate',node_id:'vid:c1',slot:'shotframe:2:end',status:'succeeded',error:'',result:{images:[{url:'https://frame',reference_url:'https://frame'}],shot_frame:{package_version:'v1',shot_id:'shot-3'}}}]);
 assert.equal(out[0].data.shotFrames['shotframe:2:end'].referenceUrl,'https://frame');
 assert.equal(out[0].data.shotFrames['shotframe:2:end'].package_version,'v1');
 assert.equal(out[0].data.prompt,'Locked draft');
});
test('prepared inherited crowd is bound even when absent from current shot list',()=>{
 const seq={id:'seq:c1',data:{kind:'sequence',sequence:{key:'c1',character_keys:[],shot_package:{materials:{crowd:{asset_id:'crowd'}}}},shots:[]}};
 const crowd={id:'asset:crowd',data:{kind:'asset',asset:{id:'crowd',key:'crowd',kind:'background_group',name:'Crowd'},plate:{referenceUrl:'https://crowd',status:'done'}}};
 store.setState({currentProjectId:null,nodes:[seq,crowd],productionAssets:[]});
 assert.equal(store.getState().collectVideoRefs('c1').refs[0].assetId,'crowd');
});
test('scene planning runs automatically and reuses a completed server job without approval',async()=>{
 calls=[];
 store.setState({currentProjectId:'raccord-film',projectRevision:0,nodes:[video],edges:[]});
 handler=async(path,body)=>{
  if(path.endsWith('/projects/raccord-film'))return {id:'raccord-film',revision:1};
  if(path.endsWith('/raccord')){assert.equal(body.sequence_key,'c1');assert.equal(body.expected_revision,1);return [{id:'plan1',kind:'raccord',status:'succeeded',result:{mode:'ai'}}];}
  if(path.endsWith('/jobs'))return [];
  throw new Error('Unexpected request '+path);
 };
 await mod.exports.ensureRaccord('c1');
 assert.equal(calls.filter(c=>c.path.endsWith('/raccord')).length,1);
 assert.equal(calls.some(c=>c.path.includes('approve')),false);
 store.setState({currentProjectId:null});
});
