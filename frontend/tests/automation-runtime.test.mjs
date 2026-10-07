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
globalThis.fetch=async(path,init={})=>{const body=init.body instanceof FormData?init.body:init.body?JSON.parse(init.body):undefined;calls.push({path,body});const out=await handler(path,body);return {ok:!out?.failure,status:out?.failure??200,json:async()=>out?.failure?{detail:out.message}:out};};
const video={id:'vid:c1',type:'autoVideo',position:{x:0,y:0},data:{kind:'video',sequenceKey:'c1',label:'Clip',title:'',prompt:'Locked draft',refs:[],status:'running',runtimeJobId:'job-1'}};
test('automatic source board keeps square settings and never primes prompts on open',async()=>{
 calls=[];
 const board={style:'donghua_premium',autoSourceFilm:true,sourceVideoId:'source-a',sourcePipeline:'one_pass',dialogueLanguage:'source',aspectRatio:'1:1',
  nodes:[{id:'char:new',type:'autoCharacter',position:{x:0,y:0},data:{kind:'character',character:{key:'new',name:'New person',states:[]},identity:{prompt:'',status:'idle'},states:{}}}],edges:[]};
 handler=async(path,body)=>path.endsWith('/projects/auto-film')?{id:'auto-film',revision:2,board}:[];
 await store.getState().openProject('auto-film');await tick();
 assert.equal(store.getState().aspectRatio,'1:1');assert.equal(store.getState().style,'donghua_premium');
 assert.equal(store.getState().sourcePipeline,'one_pass');
 assert.equal(calls.some(c=>c.path.endsWith('/prompt')),false);
 await store.getState().saveNow();
 const saved=calls.find(c=>c.path.endsWith('/projects/auto-film')&&c.body).body.board;
 assert.equal(saved.sourceVideoId,'source-a');assert.equal(saved.dialogueLanguage,'source');assert.equal(saved.autoSourceFilm,true);
 assert.equal(saved.sourcePipeline,'one_pass');
 store.getState().setStyle('anime');assert.equal(store.getState().style,'donghua_premium');
 store.setState({currentProjectId:null,autoSourceFilm:false,style:'realistic',jobs:[]});
});
test('source pipeline provenance survives file import and resets with the board',()=>{
 store.setState({currentProjectId:null});
 store.getState().importBoard(JSON.stringify({sourcePipeline:'verified_source',autoSourceFilm:true,nodes:[video],edges:[]}));
 assert.equal(store.getState().sourcePipeline,'verified_source');
 store.getState().reset();
 assert.equal(store.getState().sourcePipeline,undefined);
});
for (const presetStyle of ['donghua_premium','live_action_feature','anime_jp_modern','cartoon_us_2d']) {
test(`${presetStyle} plate request keeps video square but sends a wide character sheet`,async()=>{
 const plate={prompt:'Approved profile master',status:'idle'};
 store.setState({currentProjectId:'preset',projectRevision:0,style:presetStyle,aspectRatio:'1:1',autoSourceFilm:false,
  nodes:[{id:'char:new',type:'autoCharacter',position:{x:0,y:0},data:{kind:'character',character:{key:'new',name:'New person',states:[]},identity:plate,states:{}}}],edges:[]});
 calls=[];
 handler=async(path,body)=>{
  if(path.endsWith('/projects/preset'))return {id:'preset',revision:1};
  if(path.endsWith('/preset/jobs'))return {id:'preset-image',kind:'plate',node_id:'char:new',slot:'identity',status:'succeeded',result:{images:[{reference_url:'https://test/preset'}]}};
  return [];
 };
 await store.getState().generate('char:new','identity');
 const payload=calls.find(c=>c.path.endsWith('/preset/jobs')).body.payload;
 assert.equal(payload.style,presetStyle);assert.equal(payload.material_kind,'character');assert.equal(payload.aspect_ratio,'16:9');
 assert.deepEqual(payload.reference_urls,[]);
 assert.equal(store.getState().aspectRatio,'1:1');
 store.setState({currentProjectId:null,style:'realistic',jobs:[]});
});
}
test('choosing an approved master selects Seedream and preserves the film format',async()=>{
 calls=[];handler=async()=>[];
 store.setState({currentProjectId:null,autoSourceFilm:false,jobs:[],nodes:[],edges:[],style:'realistic',aspectRatio:'9:16',imageModel:'gemini-3-pro-image',imageSize:'1K'});
 store.getState().setStyle('cartoon_us_2d');await tick();
 assert.equal(store.getState().style,'cartoon_us_2d');
 assert.equal(store.getState().imageModel,'dola-seedream-5-0-pro');
 assert.equal(store.getState().imageSize,'2K');assert.equal(store.getState().aspectRatio,'9:16');
 assert.equal(calls.some(c=>c.path.endsWith('/plate')||c.path.endsWith('/jobs')),false);
 store.setState({style:'realistic',aspectRatio:'1:1'});
});
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
test('cinematic writer metadata survives durable job hydration',()=>{
 const seq={id:'seq:c1',data:{kind:'sequence',sequence:{key:'c1'},shots:[{duration_s:4}]}};
 const result={base_prompt:'Locked draft',prompt:'Cinematic draft',source_shots:seq.data.shots,engine:'cinematic-v1',
  staging_decisions:[{shot:1,kind:'hand_assignment',description:'Use free left hand.',basis:'Both hands start free.'}],
  reference_images:[{tag:'@image1',media_id:'ref',sha256:'hash'}]};
 const job={id:'cinematic-writer',kind:'write',node_id:'vid:c1',slot:'prompt',status:'succeeded',result};
 const out=mod.exports.applyRuntimeJobs([video,seq],[job])[0].data;
 assert.equal(out.promptEngine,'cinematic-v1');assert.deepEqual(out.stagingDecisions,result.staging_decisions);
 assert.deepEqual(out.inspectedReferences,result.reference_images);
 const edited={...video,data:{...video.data,prompt:'My changed prompt'}};
 const preserved=mod.exports.applyRuntimeJobs([edited,seq],[job])[0].data;
 assert.equal(preserved.prompt,'My changed prompt');assert.equal(preserved.promptEngine,undefined);
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
test('one-costume identity sheets bind video refs but distinct costumes require their own sheet',()=>{
 const identity={prompt:'Approved identity in green coat',status:'done',referenceUrl:'https://test/identity',mediaId:'identity-media'};
 const costume={key:'green_coat',label:'Green coat',look:'Adult lead',wardrobe:'Green wool coat',posture:''};
 const character={id:'char:hero',data:{kind:'character',character:{key:'hero',source_asset_id:'hero',name:'Hero',states:[costume]},
  activeState:costume.key,identity,states:{green_coat:{prompt:'',status:'idle'}}}};
 const seq={id:'seq:c1',data:{kind:'sequence',sequence:{key:'c1',character_keys:['hero']},
  shots:[{character_keys:['hero'],character_states:{hero:'green_coat'}}]}};
 store.setState({currentProjectId:null,nodes:[character,seq],productionAssets:[]});
 const before=structuredClone(store.getState().nodes);
 const reused=store.getState().collectVideoRefs('c1');
 assert.equal(reused.refs[0].url,identity.referenceUrl);
 assert.equal(reused.refs[0].mediaId,identity.mediaId);
 assert.equal(reused.characters[0].wardrobe,'Green wool coat');
 assert.deepEqual(store.getState().nodes,before,'binding never copies a sheet into the missing state slot');

 const multi={...character,data:{...character.data,character:{...character.data.character,states:[costume,{...costume,key:'blue_coat',wardrobe:'Blue coat'}]}}};
 store.setState({nodes:[multi,seq]});
 const missing=store.getState().collectVideoRefs('c1');
 assert.equal(missing.refs.length,0,'identity cannot stand in for one of several costumes');
 assert.equal(missing.characters[0].ref_url,undefined);
 assert.equal(missing.characters[0].wardrobe,'Green wool coat');
 const selected={prompt:'Green coat sheet',status:'done',referenceUrl:'https://test/green-coat',mediaId:'costume-media'};
 store.setState({nodes:[{...multi,data:{...multi.data,states:{green_coat:selected}}},seq]});
 assert.equal(store.getState().collectVideoRefs('c1').refs[0].url,selected.referenceUrl);
 store.setState({nodes:[{...character,data:{...character.data,states:{green_coat:selected}}},seq]});
 assert.equal(store.getState().collectVideoRefs('c1').refs[0].url,selected.referenceUrl,'a generated costume sheet takes precedence even for one costume');
});
test('server-authored video displays available sheets in inspected binding order without editing its receipt',()=>{
 const available=[
  {label:'@image1',name:'Hero',kind:'character',url:'https://test/hero',mediaId:'hero-media'},
  {label:'@image2',name:'Room',kind:'environment',url:'https://test/room',mediaId:'room-media'},
 ];
 const written={...video.data,status:'idle',refs:[],prompt:'Already reviewed server prompt',coverageToken:'unchanged-receipt',
  inspectedReferences:[{tag:'@image1',media_id:'room-media',sha256:'room-hash'},{tag:'@image2',media_id:'hero-media',sha256:'hero-hash'}]};
 const before=structuredClone({written,available});
 const display=mod.exports.videoDisplayRefs(written,available);
 assert.deepEqual(display.map(r=>[r.label,r.mediaId]),[['@image1','room-media'],['@image2','hero-media']]);
 assert.ok(display.length>0,'reference chips and video readiness do not depend on refs having been browser-authored');
 assert.deepEqual({written,available},before);
 assert.deepEqual(mod.exports.videoDisplayRefs({...written,refs:available},[]),available,'existing prompt bindings remain authoritative');
 assert.deepEqual(mod.exports.videoDisplayRefs(written,available.slice(0,1)),[],'a missing inspected image must not inherit another tag');
 assert.deepEqual(mod.exports.videoDisplayRefs(written,[...available,{...available[0],url:'https://test/ambiguous'}]),[]);
 assert.deepEqual(mod.exports.videoDisplayRefs({...written,prompt:'',inspectedReferences:undefined},available),available,'available sheets make an unwritten clip ready to write');
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

test('upload replaces a material without changing its prompt and survives historical job hydration',async()=>{
 const old={prompt:'Keep this prompt',status:'done',image:'https://old',referenceUrl:'https://old',runtimeJobId:'old-job'};
 const node={id:'env:room',data:{kind:'environment',environment:{key:'room'},plate:old}};
 const job={id:'old-job',kind:'plate',node_id:node.id,slot:'plate',status:'succeeded',result:{images:[{url:'https://old',reference_url:'https://old'}]}};
 store.setState({currentProjectId:'upload-test',projectRevision:0,nodes:[node],jobs:[job],edges:[]});
 calls=[];handler=async(path,body)=>{
  if(path.endsWith('/upload-image')){assert.ok(body instanceof FormData);return {url:'https://new',reference_url:'https://new',media_id:'new-media',persisted:true};}
  if(path.endsWith('/projects/upload-test'))return {revision:1};
  return [];
 };
 await store.getState().uploadPlate(node.id,'plate',new File(['test'],'sheet.png',{type:'image/png'}));
 const data=store.getState().nodes[0].data;
 assert.equal(data.plate.prompt,old.prompt);assert.equal(data.plate.mediaId,'new-media');
 assert.deepEqual(data.plate.ignoredRuntimeJobIds,['old-job']);
 assert.equal(mod.exports.applyRuntimeJobs(store.getState().nodes,[job])[0].data.plate.referenceUrl,'https://new');
 const newer={...job,id:'new-job',result:{images:[{url:'https://newer',reference_url:'https://newer'}]}};
 assert.equal(mod.exports.applyRuntimeJobs(store.getState().nodes,[newer])[0].data.plate.referenceUrl,'https://newer');
 assert.equal(calls.some(c=>c.path.endsWith('/jobs')||c.path.endsWith('/plate')),false);
 store.setState({currentProjectId:null,jobs:[]});
});

test('failed or late upload keeps the existing image and cannot leak across boards',async()=>{
 const node={id:'env:room',data:{kind:'environment',environment:{key:'room'},plate:{prompt:'Keep',status:'done',referenceUrl:'https://original'}}};
 store.setState({currentProjectId:'upload-failure',nodes:[node],jobs:[]});
 handler=async()=>({failure:503,message:'Storage unavailable'});
 await assert.rejects(()=>store.getState().uploadPlate(node.id,'plate',new File(['x'],'x.png')),/Storage unavailable/);
 assert.equal(store.getState().nodes[0].data.plate.referenceUrl,'https://original');
 let finish;handler=async()=>new Promise(resolve=>{finish=resolve});
 const pending=store.getState().uploadPlate(node.id,'plate',new File(['x'],'x.png'));
 await tick();store.setState({currentProjectId:'another-board',nodes:[node]});
 finish({url:'https://late',reference_url:'https://late',media_id:'late',persisted:true});await pending;
 assert.equal(store.getState().nodes[0].data.plate.referenceUrl,'https://original');
 store.setState({currentProjectId:null});
});

test('new master invalidates generated states, preserves uploaded states, and accepts matching new jobs',()=>{
 const node={id:'char:hero',data:{kind:'character',character:{key:'hero',states:[]},identity:{referenceUrl:'old-master'},states:{day:{referenceUrl:'old-state'},custom:{referenceUrl:'custom-state',uploaded:true}}}};
 const job=(id,slot,url,reference_urls=[])=>({id,kind:'plate',node_id:node.id,slot,status:'succeeded',reference_urls,result:{images:[{reference_url:url}]}});
 const old=job('old','day','old-state',['old-master']);
 const master=job('master','identity','new-master');
 const changed=mod.exports.applyRuntimeJobs([node],[old,master]);
 assert.equal(changed[0].data.states.day.needsIdentityRefresh,true);
 assert.equal(changed[0].data.states.custom.referenceUrl,'custom-state');
 assert.equal(node.data.states.day.needsIdentityRefresh,undefined);
 const polled=mod.exports.applyRuntimeJobs(changed,[old,master]);
 assert.equal(polled[0].data.states.day.needsIdentityRefresh,true);
 const fresh=mod.exports.applyRuntimeJobs(polled,[old,master,job('new','day','new-state',['new-master'])]);
 assert.equal(fresh[0].data.states.day.needsIdentityRefresh,false);
 assert.equal(fresh[0].data.states.day.referenceUrl,'new-state');
});
