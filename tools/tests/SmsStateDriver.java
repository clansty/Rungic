package com.rungic.plasma;

/** Real SMS state and validation regressions, with no Android runtime or messages. */
public class SmsStateDriver {
    private static void check(boolean ok) { if(!ok)throw new AssertionError(); }
    // covers: desktop.sms/E1 desktop.sms/E2 desktop.sms/E3
    public static void main(String[] args) {
        check(SmsState.number("+86 139-12345678").equals("+8613912345678"));
        for(String bad:new String[]{"", "12", "10000;id", "１２３", "+"}) {
            try { SmsState.number(bad);throw new AssertionError(); }
            catch(IllegalArgumentException expected) {}
        }
        check(SmsState.sameNumber("10000","10000"));
        check(!SmsState.sameNumber("10000","13912310000"));
        check(!SmsState.sameNumber("10000","BANK10000"));
        check(SmsState.sameNumber("13912345678","+8613912345678"));
        SmsState.text(new String(new int[1000],0,1000).replace('\0','a'));
        StringBuilder emoji=new StringBuilder();for(int i=0;i<1000;i++)emoji.appendCodePoint(0x1f600);
        SmsState.text(emoji.toString());
        try { SmsState.text(emoji.toString()+"x");throw new AssertionError(); }
        catch(IllegalArgumentException expected) {}
        SmsState state=new SmsState(2);
        check(state.status().equals("pending") && state.delivery().equals("unconfirmed"));
        check(!state.sent(-1,-1) && !state.sent(2,-1));
        check(state.sent(0,-1) && !state.sent(0,-1));
        check(state.sentParts()==1 && state.status().equals("pending"));
        check(state.sent(1,4));
        check(state.status().equals("failed") && state.error()==4 && state.sentParts()==1);
        check(state.receipt(0,0) && !state.receipt(0,0));
        check(state.delivery().equals("unconfirmed"));
        check(state.receipt(1,64) && state.delivery().equals("failed"));
        SmsState success=new SmsState(2);
        success.sent(0,-1);success.sent(1,-1);
        check(success.status().equals("sent"));
        check(!success.receipt(0,-1));
        success.receipt(0,0);success.receipt(1,31);
        check(success.delivery().equals("delivered"));
        SmsState waiting=new SmsState(1);waiting.receipt(0,32);
        check(waiting.delivery().equals("pending"));
        System.out.println("SMS state: validation, multipart failures, duplicate callbacks and receipts passed");
    }
}
