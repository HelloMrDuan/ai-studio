#!/usr/bin/env python3
"""Transactional installer for the V2.39.6.3 Qwen request compatibility patch."""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASELINE_VERSION = "2.39.6.3-stage04-full-pipeline-preflight"
TARGET_VERSION = "2.39.6.3-stage04-full-pipeline-preflight"
PATCH_ID = "V2.39.6.3-qwen-request-compat"
BASE_URL = "http://127.0.0.1:6008"
TARGET_REL = "app/services/gemma.py"
BASELINE_SHA256 = "daba394e03dc906be3957619bf4bb90b2980b04eafb9ce0cca5f8f5527de7876"
TARGET_SHA256 = "e50246b026bc65f4eb2b997af30004f8f9cd9f38109a06b78299a83c1ed5a4de"
TARGET_PAYLOAD = "c-rkfYjYdNk>B|%_Vz9nz=2>%e#FHDNnDAX^Oaw*e0Oyv^N<y=6gL6^1iLWB@K{C3qC`=ms90~4deE{gS+PjTb|i`q@xLV41@I|<;ku`1cV}j2K~Q#_N|jZK$(=_}Pft&GPj}DI8LihEo?*G;)sj;iaOm@(<=QX57=Ddd?nuQs7=HfFtyRO{E44$1oa&+QvtbVmH)^9s-SV)9!HeI4Ppa_7>(D$bf3-S35Dnsa^&<n+%Bt7%rCN2^iCXTrJr6p%0|Ns%q}?zI;n?{@w)X}r%M^=MYt$|lvje~R?|p9--~G?G-h2JceZ@E5{?)!Wpo97QF}wQ0t{3(mGy%^3|JncEzBh~Sy!-Z>@4UAkswV~v_-EGYo-^wF!8QjC(=-QKn>T|iEB?aM_NkNY3paw9bHV(%_NQx|mAUrDrQqyG{?)sGojMlG&bBYy4Q3t(^Yfjp2d#}O{+UbAxI1+W%3Et^+Y8IV&65NtxVz+EKi&TLcmxU{4EXc+I#190C)bSs*#Guhf7|$YV8X1|T*q@hu#0t|OWiXE&3ry@=FF;n$kLYAJlo}8lLM`-Ywe3yIbK)JwwIPWOLsd@pTfxg^xf`<ciR`%yVpMHe0DT=c<gT*XIiUs!NL}`_dmJr&)y7XF8MPzyH`JLt*y0Beg4;}qru`_=hig*;?Le~Z7#wu-J|FI>Bs&@XZ*#r;OYuZWu>)#yR&rLpPojb7oPa@S6Ztd`?E`(mHVwHr*Q7qj{`NZ|HVcB)2|WY^r`mR7LIZ4T<2?`>e2T4iO%dX01b#j3DFi1?H=9or{`L0AGH@&{S#ld?;XWaVYza&h9P-U*U+Dz^UwWJzmhkf1ar&cV)n5reJ=?tGK>TSh3Rxp&bK$#x>p|qt^E1f*6LYlso1K}?0{VD_4C2?MOq~P>1F@iXa4fXe5qE?1d9taF<jac)Bf@knIQI&x>YU9<d|6qj?dGY1*bmuPh9n9KMziw7MU4mK7E^NTdQlZti;SBDRIQ*Gr`5LiJ6FG|0sT5`z*M2L*Zo3eLG^{m~yT+UeWPU_tx3q<_5wR2<I=}71({fwRs)~{BSyC;uvwl6cIl)2W;3{J?(F-fyn6GInlXuih%!ph{&ck2?Fr-H(&krzTz)I{Js9hzITfx4^d1;GyTidB2Bil@)&rLZ{3Ycos~brUir(^HBM=u3FA3`c`3MY8>+DU$qimL{Z;qEGHf#c%#-f)9Bncj04M+WHY@=&YM+B?-{S1dxT$mUUi<hX7_xJB3c&eK9svUeXHEjw13v~AZ*d_?+j;AzzkD6nbRKy5PG{+iDqIKrt?BO4diUs`{pA@X<DR@Bt_QB-$u;N|FWbcjutDLM&f1?^>zn?=>%r8Wh=1C%kAkVA(Pzl@NBuin{w$MWp?C|KinEz0=ScDaXV7|gKiou8EPWxeap%KLlE4UaVJn#X1R-oZWa{NoaDEyH1%j5zw9i_rQ|&u*u>jZ`P4dg7VDS`@k4+LqF}1@nK06xA{S#k78|38&pZe2}2m?08nc(p{sf6~ObwDWENZs2H{PReQXpcox?R<EKCQq9U`gV^l2WOU8vtt7<<P8|0wTWYQKRMo6e#$Wmc?S0P-!>L#?SeC>{Q2Ad{d2$_utc028RGyfnufn{6WQbjY^ZzC9Ym_Xy4tyO6qqHrwguXvwXxwpc-Z;uM0?>9pYYimAO{hf7xRX)wG7TRAI<nHPulBOk@<-2*e1ZCR;Poj4?3qG2A589z6{w8*AL8q=@}AR;=V_QJ~2o7kz?`r%kI>v;PmGx>Pe8UTxdN#9We(Y_)^|b_PBxa0oW#?QlOt!pF|2%R4MKCYn;;rtrC3r6?75xi6nkOW<6cNT3M4KnlI;#;OrfL<`L|5!mqV{mPANv^}f&(wF%6+y97*lpQcYMi5n6~efbd;8nxl!Xg2`qyHhtolC(FMfk|+{ALotHrt1MsY}amlU>ks`fB#CnzL!qI08C3DUvb%c<36wuf_=ILDlNEt+uynj60EblgfL&p8<FgVZ3X<qB(MRD(0RDTvH9$%@ut-{T&|5(qY_}p?#bEcv$cM!eG~Zd>)<A1vH=3gXn&m8tF?L?78sa`iT43iI;9bN)GB^pH(aMyMeVaEznf_!(}jwv=ait38?0{g;P32`hXrQC8e_UDXPAy#9BD#Z_#U<@t_?+{TC<Ans?ADelIyn;un}W=HqM0U*6orr?AT?Yp8$2ZVU2>(ji1eG+%|$%sd*z*-y2le0miobZfsXSa2mDhs9p8??o)9Njd(l3s02rdhShiT&_3v|wZx`Sb6^O{StVNtr^0$qM6{9hypb?)#VP?()oau$t^~<0@?vZY`^gj*py=77^@`=$N*$cTY^z~aOKhBBrxG=Y#^SaG4i7ekfB$l@{O8V{<NnN5WaeE)$gX+LrTLI8L&<ez#qb(U+W^%-R0DY*T)r86bukhGRF0cRfG6;6o-?HYau_(gc(7I;2i4PrSv0~AU{IKEvkt$GIc2-(mh7sHjbdh|fkGnYMcN4?02QDc<1YB=dSG5#4Q=0$xQWhHyXiHoN>M~MEEAEIKql8jhKq!ZWG$*JX4P<6D+<kBhQE2<UswYgqg0-{W4v9rtM35D25C#^gW~q)*Z#G6$y8z{4y+5vIGH#foiXGYgGB=poTkf+>b&bUoO&h;=4aU+Hj3=C*sv|PR)rPJc=i!*(16mcvFm5}bC7Z32UZ1Gq+nom1VXH6*vMd+hWS$zKZk#of9~f$eV%?rsRjRMEpx_j#X95`p!t5||FGBVDRBPxx4->u=I4Xx>&X86efK#LN=g@K1)csa*|K4K%|?|<R015dD!7*AB92t7u|47{WT-f<cK|+y201vlS@GP#xE!!xg2aF>Rs(uERS%~3R;`K%;ty6?w(H&q6zpbSPL)v~woSo!4L2AowcV0cx64x5aIFEY%l46+Q5v!Ecf+nWQA&C?Lp4i?F{r~a;JB36aNu%z+Ne=o1$we^3vf1h|9!Jp_xC`Qr1$=z`NL#qJsrY-$lFd;0NM+j2r7X|FG%KunIINlEm*E3&TN@s5Vk5(E%o|Q!PqmPC*Y)MfT*H&gPN|p#I6ZcSYI05aSagujJuKuavo*Us#<1Da)5Y~p=|uf*fW$(omRxLeCKdze9zRa2FMuPFUmeCJ0-Rsa_Cs(o268-LDv-Yl7=uS!JM2j@Un~hh;_&r&!y~4R-L8IW;YtOhJGL7mWklvhKUD>iRCrMWy*yj1xL(RYE~J>3;V^?vX7KtJ*W|yq`$IDwX(erkufY6t7WK!X1El9X9k*Nv)eMK<2qH;Yb86wg(ng?D+3p08y7O3<y2r2H0*(0dxqH7XJ5dJpk7RV8a?&YpLsy1ebKfyY3VHon*JBUWmC%xo4>Y4M=gWwXEG7M)()=2SugUAOwjO?W+Y&7oeBRzWD8lZaO{yLM0+5iX=YU+J!-qIb%+!l*q*&%!ej{+Y!G>K@X<_jAeF+%%83p!akYJr{e(`iJyMiIB+EfRBTv!{)Xh${Zpy}H&_iAVQYD40fFOcW=cEOM4f_Gb7?8;zr3!-p857sV%=SIv0qQWMj_hjYo!F~dsBBM`o1=A?BWzdQX2UL8Zpm>9uhF5vwV{vY!FDKQ%p95vgJw3Hl!kntXWkpH(-z1H+acQrHpteDve^U+zF#%-zpFXb4C_Gk(iC|^VM3hS`k&FjP9~x5#AxqTYmHH>;{3q|+5P*b4aX<G8{JI!3uks&{13&ZQ(S@jfh&dxZh27KN|xo^{NW?Y7Ep+WYz3ii6quAZ(#y~^QO7$Sm|(+C4smxBI#o$h3JY7SpynXa#QV{vz#3Cv0gf9MKPwvTjWz$l9}R*W!H6juGN1&0BEhhP+8~3IOV)B}3k~!FSej+nIGdFrO-5gmEgs{l;j<>_!cn_83R<<2D&|>jL~+5|2l}$Qe93a{;aa7f$uh(15Yp02aC|aSDdNc>MTe#EgjzgUsg(|^sOiifx7KWwkTJMQO4aJ5nI|U2!Q&N^d>aIl$R3wxQD>mh8`GbAKZrtNFDwriYLd?mkYRRC(HW(cMsJ9P$9rNUsIvx@ITD2D=!aFQ)W(1|$pbP72-hZMm>WnZ756L@Bbwome43%tja!V|RikSfxpBg#bZ*x-<$juoaTKAUpp+0Ei5(l7NaPx4?p?lDl^G<*Qs(qc#!^V28%rX}HybOWA*~|0nPPG)H00~aU8dQTK5~J)YvfAZ_7PlD+u$KOU$g0bHH9)x$p%`yxO0{rhZI?&M=$i|a`l1EoxVFW&u(Z1#E#p(`-{$A4kq;F_6;L){$|6Y9_sA==TEL+-kEuN`|eAA;Vfs%&_fyOi{ye)uh6}o&E`>>H9&XZkwkR0OTsROJ7Gp{L^Qx+{~>;bUQhfzc6+jRq7y&dt|3&mZL8{F@ji`5moX#-DZ0A1*$1*)Je%wvTVpcg>jp`jh!T)!4VY;x9f-6jJK-UcwI4b#_`=?yfp9xW!9sCy1yLGp2g#YwnM#f4^@O2%{j-vQRYureoYFD8vvt8=xK0k@_Q%JK1ftPoV^434r6kDXjUNRY6h`K;1BSB%k+a$ag&%m>27d`X>*l0z#FcR&s4<q2`+0jbqrk^@ct$W)L=#SYu~68BD*6JF=~OB3%I*4+Oh`rsdJ;er-RVIE4fKEE!?Odqrd|;rI>RiQmlk<pax8S2P|cwh#js|&u>rL?bcwS`C2J1WY86&RWBgQfxQ4?rk7Sx{oEoeR8>k`MxF!zWYEDO@^{kn;XhlmMg~+e2UsSCz*@eRwg^Gzr@{vmZR3peqSmNO!P4V?SNN7ot1}T*+ap;==w#~HSP<HsM(t^jX@rP0)2dafHrJWF*4u4g#2&2*Ti&7f~pV?2PP7O6n-*H=E+Q|^KI8`MSt&}K(C*#vfNg4u5<XWP&5WyBdHLD^9w54yQtv0}&EYu?#hQ5g%<6|9_i|5-?#d2MPDc$`*eW%1dFYHubbgGV5EP@qT8AewcB{~mADbMV{Dm!DbX+CVpY$~1|Ej1{XERak^%*BQ_n1h`O8|D$)V%ceAjpw;%f}glSU9oO?^!%AxfN1jqK%O$5e*;b6tlClnCZ5|BFP##mWMF;)WCy+JjYmYneDq?oQDH=q{V<~QP$Wy9W)=3#H^`@NJ|7x141We4)PdJ@W7s<H06)tQJBOML&}FjsM>jBQAv#_$grzz#qO9{+!r2xW3xgDiRJb*q3vPHEG5k)*V;M~*b>*U*I;dN#d?2(pO;wm+h079F)Ee@_VJB`?F3$P0U|8M@ZcPO@ZVQkm(<5KB7-J5wD!T}fe-#UUS#dySX3!f~YkGxOa+oRtQVawtxt_Ba@G7xZ+8AWLR%ItvYs_*y-jHq5VRJ$v;pFpthS^jwd1giz;k;1f43pJ_7{)Pd$Fc%=FBYeiTjD_iw=Hi?QUiA+n2raAvdR&Mw~`L@LO>_-MHZbh3t%RYicc?G|Cdcov_U)vF$9x=@}2>i?ME3>BTZ<iMX0C6kPk#0hP#FH*sJfnZeV)K@>ciqGN#&Xo-p_d%BPygU;48*JDZ!7wP|X`5CY6fV3YO)bH6D29^>hWvZ}BQY>}eR1G|R?k`v1}>*(*yBygJ0Ms^~}pf`>5`#3B}X&>$C2-UQP>L#yLtWj$hGbI`ZQu)Ed_WC_D*?m$f$qsB!d>%2{$9{lur;Tarcw^Y)L1D%fhJJ%%j2NIznmsU4)We8vRhaJN>H*IS3EhK*nGDGpQU_c{WVuz!Rxg{-JteDd9ds&SO4)sAAOac*G-;sth}fv1&0lLU%Rr-<MvGAsW*rEQk_yP$TG>L?#%2`5!adGxh^Q!wxqN6IWs(6#nTMZyj*wIp)rjS38QP>6Kx8{=TEwU_(>DA_ZB>j}IJx6Yx^)rEUi3E~lBmFZY?dvDsoL`^t<~Aq#!Y@1Vr!wZbOs}EE7we&l#EAB#&lW!Lw5>utO){c9_D+&JonA>cpYT%9u1wu3Xx$r5I(`jm@G}*WlXXvA=EWzhlYl*m@?uVQAv4|ZH6o9ltyiDq*jiNSdje<3(lE543|NdhidF3?Y7cL4QyeTF<W_@ALgmd%=QST@;2Pjhevm85JKah$@U<J`-{JFA?Kf;^Vcqffrbc%fEpE|iim3C&BSBHVI}u;ayHC<Te0HaxaQ;WL^IYSN4!3QI-sT_nPy5_5bvT(xCs05CQ{O-F25rJrTA{6qy{;SMYULX_rV<+Ekf$ZcpZqu9BwQrq>uCp)#9P-6UFF>Xeeb|Fx=TMfICgkVTo;CHbJQ{pQuOPRaEn@bjUPbH2lT8q@?qudfh<3F3W~RZ#{~Vjd{Fr#u9vk*;&J@9k#0m-JuH}&2*+NLw^Bklr|S`a1cbn;#7NmCgP^n>RS8rwZBds>ug=^o@Cd;W@oz}pX*$n56<3gpF07<4Q~tHzYr|VkaY$?gfTi(XM!s$!R&&65-%EdPtJ8#E_N?m?X0Yzg}3%;d;KaUHv12c(Ul>*pEmtf_ws!j;V)Cy{P|<S#RvY0x!{W>YK)1;G12%mRNha_PuK;+a6WfOc;;a2Q5E*I)B-qwhind4Yh%I@Iv#^LVb}&_I+g%N)uJ+0H@hs<c~F$m5@=5gLIeggM^*^KGM0r2>2ordT;dST!bAdd!Foy<G|4m_$u$rwl;?7)s7y)ZiQAzU2Xk(~>>f0R;Z!8Jkw&c}MaIT1r9T&py}Mt0C9VfUm11_x4rJPoi8gA%bd%StSL`SSDB-FQ0!rcOKQ(DY8Z05}355YA^>us8P5?o5l5MFuK{5V$mrx@P;tmU;ccdYRZ^c7=Y+PiM=gnU8m$8!$CE0$cp+Q{rE{I_(gM-$jB1LSdpqrLU7Q+~Y4Fau;@tJ-?;}Z!FPkOHio~<;vD2)ZShW5gj3N&JI95wn#_=JHUMw2e2_#|d|IEI(>7ARAe0po7SC@lmtGuF)&3tcq0XoJEjE_odx)ZrYRT;x5Gvg<-*753KVWA^kSX%%|tTGE}9cwN!8K_aFdjTMJfvQbYLRWCPHz2>GMP4Zkl-{NWNdGxzhY$UjA=2APOPpY6@Wc@+?SzKZG53Txbd|@J3#j6^h2@;Y+&1IATq>#A}!`WZH-n}}_#T(sa(>Mg-?9*^cZbBeLJFuC0(3JL4JijIvz^UzJD>l)Qo3yFj#U!A_N~gGd@DUW)3p<%c*yR}}5_SE1OeGZdp(w!^5B-YwP`2sRz}2>eKHK@Y)PRKcY@E$0Oq&bYYCw|$-rE-@%Ury9aipZ%Df)E`DNzXTg(Vuu+4cZD$EEAPzxUod``P}dZ0^+V&?g^>IpN+=Umds^RK!azEc4>U-Me$fi+lGb$)1uFPcH`L$E*gs`IE|9=5NMqbON%4ho9&fvCBUpc~JO)Ys6{DV3`ztP&UkTSeVr8VyDoYuGrN>-Uus-mL6FF3VMiYjd3AO&8}uN21Yi$$AK`Lo3gKmm<ks1+BMLIdO$-2B8cJPX(;>6pi0Y+x}aYrg?TChKkM0nv}_o@bSt)9&+HLA6MPp*)@OA2hz_8iKJ0vb8(qoqzMC`^5cx`Fw3wzVA{{ZIrX9p`H&LaLV@UqULP}{KOE9n*Gq880PR1@{PgZ9olEP$DNj>AC&C$pC&K-AQG<N2S3!ARpP>RydcN))O=q@V4|Kw9sj{~T9)+{6gWfS3r1d|YEnUm2a1S*@%_2`CEk~?9E(naBWVa|nA(?2%$3-YNp;dtjmC&CQ<EmZ13oLz}g7c@*XRL;`+mBf3;`5%9)=k>HGNsp^Nlye}t5<zB2(+M4LvhYY=Zs~_x6p)jO{}c6~RUH?6h;*ffxgj}JIYBjf)hGqI)XO#zw!JRg=yjw?uR<BGzHaRInq{YkenpC9U|IZpnDBxcg*3XCm%E=_X)k;ooV`u&zXXe?Fa)x2y?y@zbTLx~q_>G?XM<By(1XDE8*BI&5qo(HpFN@{_2O4|7VwQV23~zjjkWY=7Vssa%a4LnbDib8o#nOmg&XXtCJh$qZ6fV$Cs0ty+fE_9>7pU&T8TrbB2t-?slM%S(KVYs5`(f0`D_9)p3ygxP-&hCc}ExI&mJOeVjzV}!`bh^Eh9z^btyK2&PEXQTOtS+%T*!3_$`ZA3V|LR#~b4Y`=Qp2f_0)O&}Ws)FZCyvdrIAB4?BnX>k$AR!AlJ{Z|^KrBB?wqq7A6ht5Wo89Dmo4y(y*%(M4p>vzH27^fYBCSpB@YZJ^Ho6dw-@rjF99it$}e5@453w$>kMeVE^Cv1ij_Twn2Mdr1BGu}1c2pg(gWxXzVG^hP5o8veTB>}+d&9UrA*dZ&B!oIita5t7yrA3ifvl?9$*L{IdA?@U(=sZ1=R0$rs7e6Arn8Df4vGim$gY)WYe7N`7kXIraN!L_B}(M)i3h4cEg+xV&@qp)~ds^ZZe9qjb%aQ@)SFH%`X5Q5o!nZ0w7G1=8~p+7r=;Vk$ALCRzzs_9pQJy4NM!;~!gGzFwPLa;#cAde#F!f0}gr0xKb;(_CkhzOYXnc}L78{ji!^mCXc6b??rGHdcDMBdy4C-|hP!6p5$GTFoQktq64Xr6p$HissYX{RO!Gc@S=oA2o4-6J2GCz9xvQ?`d<WD4{{rUIf=!2j%UZL{eq#e5}&*Mc~{iI;R@QKmwuTTY`$_`)ocry{CK6#od*JwW+jmKI3CFP5Y?Yd-lCy>>&7*MQz=pMBsj-X$5Syg8YC9S4+>?yzLiv(S8?scW674?9~Y+jmdnBdqL2O86yws}kRcQ{JUCd**2(9hijfZ=r?7mS~t1H!16`uKn)(?%ei?q{32%C@0br8qF%dX%W30MdFG7I_8wU5oY=3j1e2}<P6f)F=-#fkFmv51?tF8$}?LCLFbf;1MwU4L&<y}qz21o<uArRSo2p_(bvdOn>;c>XidIf72oK_228z}={o{7g0hAhS<=UD-O<Xu@KPU3r?K>sSB}(+Hpwfs8gnpt6De_DCvmG*2d1yfK1p4<ortl<o@H01_-Dw$!sQ413j|<?;p>mGizK*z3r-tp57q+DMYgZ_z-_vhhD$Ccs`~ts1M~!MKQ5OY@!H;RkJr@q;$WrvV=vR}!9G&XR%`7<du;_Dj$ZsCdW+eA{AFu%5nq<Re@lE=nKIrqr;LF(3pc)Xg_`b2Kkf+i(zt9ED6>;)xo1}QCJPDOIvm{7>)EBnZK`n@SlleV9p5l^xhP%=R8Wl@3Ag)VvESnwa?hl+p5~F>l-%yNFpmzwB66JdcWQTdA#<mfGIbYXJ*xpCFS=WzLF%wQ9*IbL1BuB<EY=Fuigl2IO7x%#y&$$mZCw{uuI*8>Jo`|sp+zmK)Al5)6s@vV_wX2;?60&)=m9l1%dW7Pa<SI*>dlm;rF~PBi9?0kQir7jx%Ag-4rrw{7&8(u)`OWAa9Z7+O^F<9dyyfM)=d-NHEG&ALy-5`F^at3&XE)^mLF<5Wt0Zmbr2i&vK>g0$?M6=wp~u@S+?C0C?kHsKG9#S?g2%ye*^%b<mlB~_=`G;{!v|z{-GR~`$xrnQeuo2lOIvFFvi052C=%`c4T6fD-0&r+*DyJUOv}r91YTnu93Pbb-fX_KCm1h4op^~ptM)k-Bbu7&g@2S`jaa|y@7<6>Rs(#79NsUv{2%~l=Md8Ge+r-sa`je=*~K*pbR&Pw!fAXXZ#A!UnZW*#k3UtYnZi$hXc9==IznHjv0F}^4!;HHof6pub2r}6#wgMd3VIx`_jwdb>M7%#6D7X4goV|k~#R;f@ZF}Q<$39F67Uu7yc8^#Om((Z}%p&kQ66(l!?3DlK}-a0Z^L^Oh+r>DP{FhggoZWlQ3dS09Jk>T!id47l{D4_Ij2yhtv#FGH)9hV3O>h@Vu`!(YMwxpj|F}!Lf=H%d;EWnSS#u#fKiXT_$x|hnd%4f!?Zluhp8>GJm^O_vm!aczb`^o3s%f<Po&YQ!tXe5N5To3YE)U3oKV!PaDM9xN_VvqOH$;JHnfist|~+d6{>=jUA!i4Jjn(gHQ*ec@6Qm0r{k)o)u@*VR!tjBbhzy`6KN!6*A}|I>(Wgj_oRYU~B~6WWv-j9Zp%}XEb7RqP0pH(<uvZ*r4lj4LyWxUnI)cYxN9)%rVMlb@Yn&IEY?MrXM|Ab{ZM_2<5VU1kQEE+F|yYP57N_4XZI8#<E3_$PGoeIXvtf$(SssJ@1Xy%@hdvu?Eb!C?ChgM0~Dabd%NT7mabGO0Ql%kQlFq4a&P@C&Xexo8Y03RhQG=X8qp7@LhTs_*EJu4#UEqDif)0AkBIddio8N<tg-Z7*Jl1CI&|9RG=R@<=z{tK|%9=mAp6w`7@^|3Elb>`Z3c}SFO;erLI}dLQiWv-i{!^74afisX`EyYX;FtEZBF+>)IqI@i2t;MUQ*t+x}GtDMvqi#xI;w!n#rKLzCMld{8hoWkEwNRkP|L_w%k0z34&jU-x`oI&o(^*X!NFf=Y}OiL2h<*q+Ma!zOGI*|R1S=7<wLf)V=I;J=gVK|%j|2^R)@-khty7u)|x$FM4&zZcy9T!Q<bK@z4Z7n#B0p8dy$c?eSb*Ml`l9h%p6TC1nK*FK@72F!naI!%9075`@M#RtJ0{&~9SuZ&Xk%b&kTQL*G-Ecc&Yra!n$aZi74zO#%F-mjXP#0<gWR*9jtwtKy*BgY>1=+lXIsDE%Bo>BD(3@WT*;*iXo34~X+54FRcTHEWL{|CVSi^%"
ACTIVE = {
    "starting", "warming", "queued", "switching_gpu", "running",
    "repairing", "auditing", "persisting", "generating",
}
ROOT_CANDIDATES = (
    Path("/root/autodl-tmp/ai-studio/platform-v2"),
    Path("/root/autodl-tmp/platform-v2"),
)
PYTHON_CANDIDATES = (
    Path("/root/autodl-tmp/envs/ai-studio-platform-v2/bin/python"),
    Path("/root/miniconda3/envs/ai-studio/bin/python"),
)
REQUIRED_ROOT_FILES = (
    Path("app/main.py"),
    Path("app/stage04_v238_runtime.py"),
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def target_bytes() -> bytes:
    data = zlib.decompress(base64.b85decode(TARGET_PAYLOAD))
    require(sha(data) == TARGET_SHA256, "embedded target SHA256 mismatch")
    return data


def unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if str(resolved) not in seen:
            result.append(resolved)
            seen.add(str(resolved))
    return result


def valid_root(root: Path) -> bool:
    return root.is_dir() and all((root / rel).is_file() for rel in REQUIRED_ROOT_FILES)


def discover_root(override: Path | None = None) -> Path:
    candidates = unique_paths(([override] if override else []) + list(ROOT_CANDIDATES))
    for candidate in candidates:
        if valid_root(candidate):
            return candidate
    raise RuntimeError(
        "platform root candidates checked:\n"
        + "\n".join(str(path) for path in candidates)
        + "\nnot found"
    )


def python_usable(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        result = subprocess.run(
            [str(path), "--version"],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def discover_python(override: Path | None = None) -> Path:
    candidates = unique_paths(
        ([override] if override else []) + list(PYTHON_CANDIDATES) + [Path(sys.executable)]
    )
    for candidate in candidates:
        if python_usable(candidate):
            return candidate
    raise RuntimeError(
        "platform Python candidates checked:\n"
        + "\n".join(str(path) for path in candidates)
        + "\nnot found or cannot execute --version"
    )


def request_json(path: str, timeout: float = 20) -> tuple[int, Any]:
    request = urllib.request.Request(BASE_URL + path, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return exc.code, {"raw": exc.read().decode("utf-8", errors="replace")}


def run(command: list[str], timeout: float) -> None:
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {command!r}\n"
            f"stdout={result.stdout[-3000:]}\nstderr={result.stderr[-3000:]}"
        )


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def parse_data_dir(root: Path) -> Path:
    env_path = root / ".env"
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            if raw.strip().startswith("DATA_DIR="):
                value = raw.split("=", 1)[1].strip().strip('"').strip("'")
                path = Path(value)
                return path if path.is_absolute() else root / path
    return root / "data"


def iter_status_rows(value: Any):
    if isinstance(value, dict):
        if "status" in value:
            yield value
        for child in value.values():
            yield from iter_status_rows(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_status_rows(child)


def check_active_tasks() -> None:
    status, projects = request_json("/api/studio/projects")
    require(status == 200 and isinstance(projects, list), "cannot inspect Studio projects")
    for item in projects:
        project_id = str((item or {}).get("project_id") or (item or {}).get("id") or "")
        if not project_id:
            continue
        status, row = request_json(
            f"/api/studio/projects/{project_id}/stage04/rebuild-production/status"
        )
        require(status == 200, f"cannot inspect Stage04 task: {project_id}")
        require(
            str((row or {}).get("status") or "").lower() not in ACTIVE,
            f"active Stage04 task: {project_id}",
        )


def check_active_task_files(root: Path) -> None:
    patterns = (
        "stage04_rebuild_tasks/*.json",
        "tasks/*/task.json",
        "studio_jobs/*.json",
        "studio_video_edit_jobs/*.json",
        "director_workbench_candidates/*.json",
    )
    for pattern in patterns:
        for path in parse_data_dir(root).glob(pattern):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"cannot inspect task state {path}: {exc}") from exc
            for row in iter_status_rows(value):
                require(
                    str(row.get("status") or "").lower() not in ACTIVE,
                    f"active task in {path}",
                )


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_name(path.name + ".qwen-request-compat.tmp")
    temporary.write_bytes(data)
    os.chmod(temporary, mode)
    temporary.replace(path)


def backup_live(root: Path, backup: Path) -> dict[str, Any]:
    source = root / TARGET_REL
    require(source.is_file(), f"baseline file missing: {TARGET_REL}")
    data = source.read_bytes()
    require(sha(data) == BASELINE_SHA256, f"baseline SHA256 mismatch: {TARGET_REL}")
    mode = os.stat(source).st_mode & 0o777
    destination = backup / TARGET_REL
    destination.parent.mkdir(parents=True, exist_ok=False)
    destination.write_bytes(data)
    os.chmod(destination, mode)
    manifest = {
        "patch_id": PATCH_ID,
        "baseline_version": BASELINE_VERSION,
        "target_version": TARGET_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {
            TARGET_REL: {
                "before_sha256": sha(data),
                "target_sha256": TARGET_SHA256,
                "mode": mode,
            }
        },
    }
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def restore_exact_backup(root: Path, backup: Path, manifest: dict[str, Any]) -> None:
    item = manifest["files"][TARGET_REL]
    data = (backup / TARGET_REL).read_bytes()
    require(sha(data) == item["before_sha256"], f"backup corrupted: {TARGET_REL}")
    atomic_write(root / TARGET_REL, data, int(item["mode"]))
    require(
        sha((root / TARGET_REL).read_bytes()) == item["before_sha256"],
        f"rollback hash mismatch: {TARGET_REL}",
    )


def stop_platform(root: Path) -> None:
    run(["bash", str(root / "scripts/stop.sh")], 60)
    deadline = time.monotonic() + 20
    while port_open(6008) and time.monotonic() < deadline:
        time.sleep(1)
    require(not port_open(6008), "port 6008 still listening after stop")


def start_and_verify(root: Path, expected_version: str) -> None:
    run(["bash", str(root / "scripts/start.sh")], 120)
    deadline = time.monotonic() + 120
    last = ""
    while time.monotonic() < deadline:
        try:
            status, health = request_json("/api/health", 30)
            if status == 200:
                require(health.get("version") == expected_version, "health runtime version mismatch")
                status, schema = request_json("/openapi.json", 20)
                require(
                    status == 200 and schema.get("info", {}).get("version") == expected_version,
                    "OpenAPI version mismatch",
                )
                return
            last = f"HTTP {status}: {health}"
        except Exception as exc:
            last = str(exc)
        time.sleep(2)
    raise RuntimeError(f"platform health timeout: {last}")


def self_test() -> int:
    data = target_bytes()
    ast.parse(data.decode("utf-8"), filename=TARGET_REL)
    compile(data, TARGET_REL, "exec")
    source = data.decode("utf-8")
    for marker in (
        "QWEN_RUNTIME_MODEL",
        "_normalize_request_messages",
        "_normalize_runtime_model",
        "Qwen request rejected",
        '"request_attempts": attempt + 1',
        '"usage"',
        '"timings"',
        'response_model = _text(body.get("model"))',
    ):
        require(marker in source, f"target marker missing: {marker}")
    request_source = ast.get_source_segment(
        source,
        next(
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.ClassDef)
            for node in node.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_request_messages"
        ),
    )
    require(request_source is not None, "_request_messages source unavailable")
    require("reasoning_effort" not in request_source, "historical reasoning parameter remains")
    require("chat_template_kwargs" not in request_source, "historical template parameter remains")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "root"
        backup = Path(directory) / "backup"
        live = root / TARGET_REL
        saved = backup / TARGET_REL
        live.parent.mkdir(parents=True)
        saved.parent.mkdir(parents=True)
        original = b"exact-live-before"
        live.write_bytes(original)
        saved.write_bytes(original)
        manifest = {
            "files": {
                TARGET_REL: {
                    "before_sha256": sha(original),
                    "target_sha256": TARGET_SHA256,
                    "mode": 0o644,
                }
            }
        }
        atomic_write(live, data, 0o644)
        restore_exact_backup(root, backup, manifest)
    print("INSTALLER SELF-TEST PASS")
    print("TARGET AST/PAYLOAD PASS")
    print("ROLLBACK SIMULATION PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, help="platform root override")
    parser.add_argument("--python", type=Path, help="platform Python override")
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path("/root/autodl-tmp/ai-studio/backups"),
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    root = discover_root(args.root)
    platform_python = discover_python(args.python)
    print(f"PATCH_ID={PATCH_ID}")
    print(f"PLATFORM_ROOT={root}")
    print(f"PLATFORM_PYTHON={platform_python}")
    status, schema = request_json("/openapi.json")
    require(
        status == 200 and schema.get("info", {}).get("version") == BASELINE_VERSION,
        "baseline OpenAPI version mismatch",
    )
    check_active_tasks()
    backup = args.backup_root / (
        "platform-v2-v23963-qwen-request-compat-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    manifest = backup_live(root, backup)
    applied = False
    platform_stopped = False
    try:
        stop_platform(root)
        platform_stopped = True
        check_active_task_files(root)
        applied = True
        atomic_write(
            root / TARGET_REL,
            target_bytes(),
            int(manifest["files"][TARGET_REL]["mode"]),
        )
        run([str(platform_python), "-m", "py_compile", str(root / TARGET_REL)], 240)
        start_and_verify(root, TARGET_VERSION)
        require(
            sha((root / TARGET_REL).read_bytes()) == TARGET_SHA256,
            f"target hash readback mismatch: {TARGET_REL}",
        )
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["result"] = "INSTALLED"
        (backup / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(f"BACKUP={backup}")
        print("INSTALL PASS; no Stage04, image, video or composition task was executed")
        return 0
    except Exception:
        if applied:
            try:
                if port_open(6008):
                    stop_platform(root)
            except Exception as exc:
                print(f"ROLLBACK STOP WARNING: {exc}", file=sys.stderr)
            restore_exact_backup(root, backup, manifest)
        if platform_stopped:
            try:
                start_and_verify(root, BASELINE_VERSION)
            except Exception as exc:
                print(f"ROLLBACK RESTORED FILE BUT RESTART FAILED: {exc}", file=sys.stderr)
        if applied:
            print(f"ROLLBACK COMPLETE FROM EXACT LIVE BACKUP {backup}", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"INSTALL FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
