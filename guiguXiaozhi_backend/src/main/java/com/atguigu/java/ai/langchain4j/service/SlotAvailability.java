package com.atguigu.java.ai.langchain4j.service;

import java.util.List;

/**
 * 号源查询结果。
 *
 * @param available 是否有可预约号源
 * @param message   可直接转述给用户的中文描述
 * @param doctors   当日出诊且有号的医生
 * @param remaining 当日剩余号源总数
 */
public record SlotAvailability(boolean available, String message, List<String> doctors, int remaining) {

    public static SlotAvailability none(String message) {
        return new SlotAvailability(false, message, List.of(), 0);
    }
}
