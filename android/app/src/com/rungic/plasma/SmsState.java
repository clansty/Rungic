package com.rungic.plasma;

import java.util.Arrays;

/** SMS validation and per-part radio/receipt state, without Android dependencies. */
final class SmsState {
    static final int MAX_TEXT=1000, UNANSWERED=Integer.MIN_VALUE;
    static String number(String raw) {
        String value=raw==null?"":raw.replaceAll("[\\s-]","");
        if(!value.matches("\\+?[0-9]{3,20}"))throw new IllegalArgumentException("Invalid phone number");
        return value;
    }
    static boolean sameNumber(String expected,String actual) {
        String wanted=number(expected).replace("+","");
        String received=actual==null?"":actual.replaceAll("[\\s-]","");
        if(!received.matches("\\+?[0-9]{3,20}"))return false;
        received=received.replace("+","");
        // Service short codes must match exactly; international prefixes are permitted for
        // subscriber numbers only, never e.g. 13912310000 matching service number 10000.
        return wanted.equals(received) || (Math.min(wanted.length(),received.length())>=7 &&
                (wanted.endsWith(received) || received.endsWith(wanted)));
    }
    static void text(String value) {
        if(value==null || value.isEmpty() || value.codePointCount(0,value.length())>MAX_TEXT)
            throw new IllegalArgumentException("Text must be 1 to "+MAX_TEXT+" characters");
    }
    private final int[] sent, receipt;
    SmsState(int parts) {
        if(parts<1)throw new IllegalArgumentException("No SMS parts");
        sent=new int[parts];receipt=new int[parts];
        Arrays.fill(sent,UNANSWERED);Arrays.fill(receipt,UNANSWERED);
    }
    synchronized boolean sent(int part,int code) {
        if(part<0 || part>=sent.length || sent[part]!=UNANSWERED)return false;
        sent[part]=code;return true;
    }
    synchronized boolean receipt(int part,int status) {
        if(part<0 || part>=receipt.length || status<0 || receipt[part]!=UNANSWERED)return false;
        receipt[part]=status;return true;
    }
    synchronized int sentParts() { int n=0;for(int code:sent)if(code==-1)n++;return n; }
    synchronized int error() { for(int code:sent)if(code!=UNANSWERED && code!=-1)return code;return -1; }
    synchronized String status() {
        if(error()!=-1)return "failed";
        return sentParts()==sent.length?"sent":"pending";
    }
    synchronized String delivery() {
        boolean unknown=false,pending=false;
        for(int code:receipt) {
            if(code==UNANSWERED)unknown=true;
            else if(code>=64)return "failed";
            else if(code>=32)pending=true;
        }
        return unknown?"unconfirmed":pending?"pending":"delivered";
    }
}
