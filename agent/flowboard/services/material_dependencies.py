"""Separate existing co-references from prerequisites for generating missing sheets."""


def graph(assets, ready):
    edges={aid:set(a.get('generation_depends_on_asset_ids',a.get('depends_on_asset_ids',[])))|set(a.get('member_ids',[])) for aid,a in assets.items()}
    # Tarjan components: no lexical guesses about bottles, liquids, costumes or film names.
    index=0;indices={};low={};stack=[];active=set();components=[]
    def visit(v):
        nonlocal index
        indices[v]=low[v]=index;index+=1;stack.append(v);active.add(v)
        for w in sorted(edges.get(v,set())):
            if w not in indices:visit(w);low[v]=min(low[v],low[w])
            elif w in active:low[v]=min(low[v],indices[w])
        if low[v]==indices[v]:
            group=[]
            while True:
                w=stack.pop();active.remove(w);group.append(w)
                if w==v:break
            components.append(group)
    for aid in sorted(edges):
        if aid not in indices:visit(aid)
    related={};notes=[]
    for members in components:
        group=set(members)
        if len(group)==1 and not group & edges.get(members[0],set()):continue
        hard=any((set(assets.get(a,{}).get('generation_depends_on_asset_ids',[]))|set(assets.get(a,{}).get('member_ids',[]))) & group for a in group)
        if hard or not all(ready.get(a) for a in group):
            raise ValueError('Cyclic material dependency requires explicit generation order or existing references: '+', '.join(sorted(group)))
        # All members already exist. Their legacy relation is retained as context,
        # but cannot force the scheduler to regenerate one merely to unlock another.
        for a in group:
            edges[a]-=group;related[a]=sorted(group-{a})
        notes.append({'code':'existing_coreference_group','assets':sorted(group),
                      'message':'Existing references resolve this legacy cycle; no generation order inferred.'})
    return edges,related,notes
