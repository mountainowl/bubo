// Run against a real ui-export data.json before publishing a preview.
import {build} from 'vite';
import {svelte} from '@sveltejs/vite-plugin-svelte';
import {render} from 'svelte/server';
import {readFileSync,mkdtempSync,rmSync} from 'node:fs';
import {fileURLToPath,pathToFileURL} from 'node:url';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('..',import.meta.url));
const out=mkdtempSync(root+'/.render-check-');
const data=JSON.parse(readFileSync(process.argv[2],'utf8'));
try {
 await build({configFile:false,root,logLevel:'error',plugins:[svelte()],build:{ssr:true,outDir:out,emptyOutDir:true,rollupOptions:{input:{Dashboard:root+'/src/views/Dashboard.svelte',Reports:root+'/src/views/Reports.svelte',Reviews:root+'/src/views/Reviews.svelte',Charts:root+'/src/components/DashboardCharts.svelte'}}}});
 const components={};
 for(const name of ['Dashboard','Reports','Reviews','Charts'])components[name]=(await import(pathToFileURL(out+'/'+name+'.js'))).default;
 const m=data.dashboard.metrics.windows[0];
 assert.equal(m.production.runs,0);assert(m.validation.runs>0);
 for(const name of ['Dashboard','Reports','Reviews']) {
  const html=render(components[name],{props:{data,onopen:()=>{},onreviews:()=>{}}}).body;
  assert(!html.includes('NaN')&&!html.includes('Infinity'));
  assert(!html.includes('Sample data'));assert(!html.includes('class="validation"'));
  if(name==='Dashboard') {
   assert(html.includes(`${m.validation.runs} dry-run executions; no reviews posted.`));
   assert(html.includes(`Review execution: Successful ${m.validation.successful}, Failed ${m.validation.failed}`));
   assert.equal((html.match(/<article[^>]*class="queue-row/g)||[]).length,5);
  }
  if(name==='Reports')assert(html.includes('Dry-run runs started in this period.'));
  console.log(`${name}: real data rendered in existing layout PASS`);
 }
 for(const scope of ['production','dry-run']) {
  const html=render(components.Charts,{props:{metric:m,scope}}).body;
  assert(html.includes(scope==='dry-run'?`${m.validation.runs} dry-run executions`:'0 production runs'));
  assert(!html.includes('NaN'));
 }
 const empty=render(components.Dashboard,{props:{data:{meta:{},dashboard:{recent:[],metrics:{windows:[]}}},onopen:()=>{}}}).body;
 assert(empty.includes('No local review history'));assert(!empty.includes('NaN'));
 console.log('Explicit production/dry-run selection and empty export PASS');
} finally {rmSync(out,{recursive:true,force:true});}
