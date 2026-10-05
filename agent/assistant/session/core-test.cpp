// SPDX-License-Identifier: GPL-2.0-or-later
// covers: agent.phone-mode/E2 agent.phone-mode/E3 agent.phone-mode/E5 agent.phone-mode/E7 agent.phone-mode/E12
#include "core.h"
#include <QCoreApplication>
#include <cstdio>
#include <cstdlib>
static void check(bool ok,const char *what){if(!ok){std::fprintf(stderr,"FAIL: %s\n",what);std::exit(1);}}
int main(int argc,char **argv){QCoreApplication app(argc,argv);
    Tasks t;auto a=t.add("count",true,"chat","a"),b=t.add("weather",true,"chat","b");
    check(t.add("duplicate",false,"chat","a")==a,"deduplicate tool calls");
    check(t.rows.size()==2,"no duplicate side effects");check(t.schedule().size()==2,"independent reads run concurrently");
    auto c=t.add("edit",false,"chat","c"),d=t.add("research",true,"chat","d");check(t.schedule().isEmpty(),"two workers maximum");
    t.find(a)->status="completed";check(t.schedule().isEmpty(),"exclusive waiter prevents starvation");
    t.stop(b);check(t.find(b)->status=="stopping","stop waits for actual completion");check(t.schedule().isEmpty(),"stopping work still owns resources");
    t.find(b)->status="stopped";check(t.schedule()==QList<QString>{c},"exclusive work runs alone");check(t.schedule().isEmpty(),"read waits during mutation");
    t.stop(d);check(t.find(d)->status=="stopped","queued work stops without a turn");
    t.find(c)->status="completed";check(t.snapshot("other").isEmpty(),"history stays in originating conversation");
    t.focused.clear();check(t.target("")==nullptr,"no implicit target when all tasks complete");
    Tasks ambiguous;ambiguous.add("one",true,"chat","1");ambiguous.add("two",true,"chat","2");ambiguous.focused.clear();check(!ambiguous.target(""),"ambiguous cancellation needs a target");
    Tasks recovered;recovered.restore(ambiguous.snapshot());check(recovered.rows[0].status=="interrupted","restart never executes queued work");check(recovered.schedule().isEmpty(),"no replay after restart");
    Tasks bounded;for(int i=0;i<128;++i)check(!bounded.add("queued",true,"chat",QString::number(i)).isEmpty(),"queue accepts its bound");
    check(bounded.add("overflow",true,"chat","overflow").isEmpty(),"full live queue rejects further work");
    bounded.rows[0].status="completed";check(!bounded.add("replacement",true,"chat","new").isEmpty()&&bounded.rows.size()==128,"terminal history can be evicted without dropping live work");
    ReplyBuffer audio;check(audio.append("response1","item1",QByteArray(4800,'a')),"first PCM accepted");audio.clear();auto e=audio.generation;
    audio.append("response1","item1",QByteArray(4800,'b'));check(audio.pending.isEmpty(),"late cancelled PCM discarded");check(audio.generation==e,"late packet cannot resurrect generation");
    audio.append("response2","item2",QByteArray(4800,'c'));check(audio.pending.size()==4800,"new reply plays");audio.append("response1","item1",QByteArray(200,'x'));check(audio.response=="response2"&&audio.pending.size()==4800,"old reply cannot replace a newer reply");check(audio.append("response2","item2",QByteArray(24000*2*120,'d')),"a two-minute reply ahead of playback is kept");check(!audio.append("response2","item2",QByteArray(ReplyBuffer::MaxPendingBytes,'d')),"playback memory bounded");check(audio.full&&audio.append("response2","item2",QByteArray(960,'e'))&&audio.pending.size()==4800+24000*2*120,"past the bound the rest of the reply is dropped once, not refused again");audio.append("response3","item3",QByteArray(960,'f'));check(!audio.full&&audio.pending.size()==960,"the next reply starts unbounded again");
    std::puts("session-core: scheduling, cancellation, bounds and playback checks passed");return 0;
}
